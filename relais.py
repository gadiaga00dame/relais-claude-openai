"""Cœur du relais entre Claude (client MCP) et un agent OpenAI.

Ce module ne dépend ni du protocole MCP ni de la bibliothèque OpenAI : l'agent
est injecté sous la forme d'un « adaptateur » (voir AdaptateurAgent), ce qui
permet de tester toute la logique avec un agent simulé.

Responsabilités :
- récupérer un fichier sur GitHub à un commit précis et calculer son empreinte ;
- transmettre consigne et fichier à l'agent, dans un fil dont l'historique est conservé ;
- établir la preuve d'exécution à partir des seules traces de l'outil d'exécution,
  en distinguant fichier vérifié, exécution effectuée et exécution réussie ;
- réserver la consommation AVANT chaque appel (tâches simultanées comprises),
  puis l'ajuster à la consommation réelle ;
- appliquer une échéance stricte : une réponse tardive n'est jamais présentée comme terminée ;
- tenir un journal horodaté et chaîné de chaque échange.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from config import Config

# ---------------------------------------------------------------------------
# Types échangés avec l'adaptateur
# ---------------------------------------------------------------------------


@dataclass
class AppelCode:
    """Une exécution réalisée par l'outil d'exécution de l'agent (trace brute)."""

    code: str
    sorties: list[str]
    conteneur: str | None = None


@dataclass
class ResultatAgent:
    identifiant_reponse: str
    texte: str
    appels_code: list[AppelCode]
    jetons_entree: int
    jetons_sortie: int
    statut: str = "completed"
    usage_connue: bool = True    # False : consommation non rapportée, la réservation sera comptée


class DelaiDepasse(Exception):
    """Levée par l'adaptateur quand l'échéance est atteinte avant la fin de la réponse."""

    def __init__(self, identifiant_reponse: str | None, annulation_demandee: bool,
                 detail: str = "", arret_confirme: bool = False):
        super().__init__(detail or "échéance atteinte")
        self.identifiant_reponse = identifiant_reponse
        self.annulation_demandee = annulation_demandee
        self.arret_confirme = arret_confirme  # le fournisseur a confirmé l'état final « cancelled »
        self.detail = detail


class AdaptateurAgent(Protocol):
    def preparer_conteneur(
        self, conteneur: str | None, nom_fichier: str, contenu: bytes
    ) -> tuple[str, str]:
        """Crée ou réutilise un conteneur, y dépose le fichier.

        Renvoie (identifiant_conteneur, chemin_du_fichier_dans_le_conteneur).
        """

    def repondre(
        self,
        *,
        instructions: str,
        message: str,
        conteneur: str | None,
        reponse_precedente: str | None,
        max_jetons_sortie: int,
        max_appels_outil: int,
        delai_secondes: float,
    ) -> ResultatAgent:
        """Envoie le message à l'agent et renvoie sa réponse avec les traces d'exécution.

        Doit lever DelaiDepasse si la réponse n'est pas finie dans delai_secondes
        (en tentant d'annuler la réponse côté fournisseur).
        """


class ErreurRelais(Exception):
    """Refus explicite (quota, paramètre invalide, fil occupé…)."""


# ---------------------------------------------------------------------------
# Constantes et utilitaires
# ---------------------------------------------------------------------------

INSTRUCTIONS_AGENT = """\
Tu es l'agent de revue critique et de validation d'un projet de calcul scientifique
et de science des données. Ton interlocuteur est Claude, qui écrit principalement le code.
Réponds en français, de façon précise et argumentée.

Règles :
1. Quand un fichier est joint, exécute d'abord EXACTEMENT le bloc de vérification fourni,
   avec l'outil python, en un seul appel et sans le modifier. Ne prétends jamais avoir
   exécuté du code sans l'avoir fait réellement avec l'outil python.
2. Distingue toujours ce que tu as observé à l'exécution de ce que tu déduis.
3. Pour chaque objection : localisation, gravité (bloquante / importante / mineure),
   justification mathématique ou technique, correction proposée, test permettant de trancher.
4. Si tu es d'accord, dis-le simplement ; ne crée pas d'objections pour la forme.
"""

MARQUEUR_EMPREINTE = "EMPREINTE_CONTENEUR="
_RE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RE_DEPOT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RE_FIL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def jour_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def empreinte(contenu: bytes) -> str:
    return hashlib.sha256(contenu).hexdigest()


# ---------------------------------------------------------------------------
# Récupération du fichier partagé
# ---------------------------------------------------------------------------


def valider_reference(config: Config, depot: str, chemin: str, commit: str) -> None:
    if not _RE_DEPOT.match(depot):
        raise ErreurRelais(f"Nom de dépôt invalide : {depot!r} (attendu : proprietaire/depot).")
    if config.depots_autorises and depot not in config.depots_autorises:
        raise ErreurRelais(f"Dépôt non autorisé : {depot}. Autorisés : {', '.join(config.depots_autorises)}.")
    if not _RE_COMMIT.match(commit):
        raise ErreurRelais(
            "Le commit doit être un SHA complet de 40 caractères hexadécimaux "
            "(un nom de branche ne garantit pas la même version des deux côtés)."
        )
    if chemin.startswith("/") or ".." in chemin.split("/") or chemin.strip() == "":
        raise ErreurRelais(f"Chemin de fichier invalide : {chemin!r}.")


def telecharger_depuis_github(config: Config, depot: str, chemin: str, commit: str) -> bytes:
    """Télécharge le fichier brut au commit indiqué (requests importé ici pour faciliter les tests)."""
    import requests

    url = f"https://raw.githubusercontent.com/{depot}/{commit}/{chemin}"
    entetes = {"Authorization": f"Bearer {config.jeton_github}"} if config.jeton_github else {}
    reponse = requests.get(url, headers=entetes, timeout=30, stream=True)
    if reponse.status_code == 404:
        raise ErreurRelais(f"Fichier introuvable sur GitHub : {depot}@{commit[:12]}:{chemin}.")
    reponse.raise_for_status()
    contenu = reponse.raw.read(config.taille_max_fichier + 1, decode_content=True)
    if len(contenu) > config.taille_max_fichier:
        raise ErreurRelais(f"Fichier trop volumineux (limite : {config.taille_max_fichier} octets).")
    return contenu


# ---------------------------------------------------------------------------
# Preuve d'exécution
# ---------------------------------------------------------------------------


def bloc_verification(chemin_conteneur: str, jeton: str, delai_execution: float) -> str:
    """Code que l'agent doit exécuter tel quel.

    - le jeton est propre à la tâche : une sortie d'une tâche antérieure ne peut pas être réutilisée ;
    - le résultat de l'exécution est encodé en JSON sur une seule ligne : ce que le fichier
      affiche lui-même ne peut pas imiter les lignes de contrôle.
    """
    return (
        "import hashlib, json, subprocess, sys\n"
        f"CHEMIN = {chemin_conteneur!r}\n"
        f"JETON = {jeton!r}\n"
        "print('DEBUT_VERIFICATION=' + JETON)\n"
        f"print({MARQUEUR_EMPREINTE!r} + hashlib.sha256(open(CHEMIN, 'rb').read()).hexdigest())\n"
        "try:\n"
        f"    r = subprocess.run([sys.executable, CHEMIN], capture_output=True, text=True, timeout={delai_execution!r})\n"
        "    resultat = {'code_retour': r.returncode, 'sortie': r.stdout, 'erreurs': r.stderr[-4000:]}\n"
        "except subprocess.TimeoutExpired:\n"
        "    resultat = {'code_retour': 'DELAI_DEPASSE', 'sortie': '', 'erreurs': ''}\n"
        "print('RESULTAT_' + JETON + '=' + json.dumps(resultat))\n"
        "print('FIN_VERIFICATION=' + JETON)\n"
    )


def _normaliser(code: str) -> str:
    return "\n".join(ligne.rstrip() for ligne in code.strip().replace("\r\n", "\n").split("\n"))


def verifier_preuve(
    appels: list[AppelCode],
    empreinte_attendue: str,
    chemin_conteneur: str,
    jeton: str,
    bloc_attendu: str,
) -> dict:
    """Établit la preuve à partir des seules traces de l'outil d'exécution.

    Seul le bloc de contrôle exact de CETTE tâche (même code, même jeton) fait foi.
    Trois constats distincts :
    - fichier   : « verifie » (empreinte identique), « discordant », « non_verifie » ;
    - execution : « effectuee » (bloc conforme exécuté jusqu'à sa dernière ligne),
                  « incomplete » (bloc conforme, trace interrompue), « non_conforme »
                  (code modifié ou trace d'une autre tâche), « absente » ;
    - reussite  : « reussie » (code retour 0), « echouee », « delai_depasse », « inconnue ».

    statut_global :
    - « verifiee »          : fichier vérifié ET exécution effectuée (la réussite est donnée à part) ;
    - « fichier_seulement » : fichier vérifié, exécution non établie ;
    - « discordante »       : le fichier exécuté n'est pas celui du commit ;
    - « absente »           : rien n'est établi.
    Une lecture du fichier hors bloc de contrôle est signalée pour information
    (« lecture_hors_controle ») mais ne vérifie rien.
    """
    preuve = {
        "statut_global": "absente",
        "fichier": {"statut": "non_verifie", "empreinte_serveur": empreinte_attendue,
                    "empreinte_conteneur": None, "lecture_hors_controle": None},
        "execution": {"statut": "absente", "bloc_conforme": False, "conteneur": None},
        "reussite": "inconnue",
        "code_retour": None,
        "sortie_execution": None,
        "erreurs_execution": None,
        "nb_appels_outil": len(appels),
    }
    attendu = _normaliser(bloc_attendu)

    def lire_empreinte(lignes: list[str]) -> str | None:
        ligne = next((l for l in lignes if l.startswith(MARQUEUR_EMPREINTE)), None)
        return None if ligne is None else ligne[len(MARQUEUR_EMPREINTE):].strip()

    for appel in appels:
        lignes = "\n".join(appel.sorties).splitlines()
        if _normaliser(appel.code) != attendu:
            if chemin_conteneur in appel.code or jeton in appel.code:
                if preuve["execution"]["statut"] == "absente":
                    preuve["execution"]["statut"] = "non_conforme"
                observee = lire_empreinte(lignes)
                if observee is not None:
                    preuve["fichier"]["lecture_hors_controle"] = observee
            continue

        preuve["execution"]["bloc_conforme"] = True
        preuve["execution"]["conteneur"] = appel.conteneur
        if f"DEBUT_VERIFICATION={jeton}" not in lignes:
            preuve["execution"]["statut"] = "incomplete"
            continue
        observee = lire_empreinte(lignes)
        if observee is not None:
            preuve["fichier"]["empreinte_conteneur"] = observee
            preuve["fichier"]["statut"] = "verifie" if observee == empreinte_attendue else "discordant"

        prefixe = f"RESULTAT_{jeton}="
        ligne_resultat = next((l for l in lignes if l.startswith(prefixe)), None)
        resultat = None
        if ligne_resultat is not None and f"FIN_VERIFICATION={jeton}" in lignes:
            try:
                resultat = json.loads(ligne_resultat[len(prefixe):])
            except json.JSONDecodeError:
                resultat = None
        if observee is None or not isinstance(resultat, dict):
            preuve["execution"]["statut"] = "incomplete"
            continue

        preuve["execution"]["statut"] = "effectuee"
        code = resultat.get("code_retour")
        preuve["code_retour"] = code
        preuve["sortie_execution"] = resultat.get("sortie")
        preuve["erreurs_execution"] = resultat.get("erreurs")
        if code == "DELAI_DEPASSE":
            preuve["reussite"] = "delai_depasse"
        elif isinstance(code, int) and not isinstance(code, bool):
            preuve["reussite"] = "reussie" if code == 0 else "echouee"
        break

    fichier, execution = preuve["fichier"]["statut"], preuve["execution"]["statut"]
    if fichier == "discordant":
        preuve["statut_global"] = "discordante"
    elif fichier == "verifie" and execution == "effectuee":
        preuve["statut_global"] = "verifiee"
    elif fichier == "verifie":
        preuve["statut_global"] = "fichier_seulement"
    return preuve


# ---------------------------------------------------------------------------
# Stockage (SQLite)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fils (
    fil TEXT PRIMARY KEY,
    reponse_precedente TEXT,
    conteneur TEXT,
    cree_le TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS taches (
    tache TEXT PRIMARY KEY,
    fil TEXT NOT NULL,
    statut TEXT NOT NULL,            -- en_cours | terminee | echec | expiree | interrompue
    debut TEXT NOT NULL,
    echeance TEXT NOT NULL,
    fin TEXT,
    resultat TEXT,                   -- JSON, uniquement si terminée dans les délais
    resultat_tardif TEXT,            -- JSON d'une réponse arrivée après l'échéance
    jour TEXT NOT NULL,
    jetons_reserves INTEGER NOT NULL,
    executions_reservees INTEGER NOT NULL,
    ajuste INTEGER NOT NULL DEFAULT 0,
    jetons_reels INTEGER
);
CREATE TABLE IF NOT EXISTS journal (
    numero INTEGER PRIMARY KEY AUTOINCREMENT,
    fil TEXT NOT NULL,
    horodatage TEXT NOT NULL,
    auteur TEXT NOT NULL,            -- claude | agent | relais
    tache TEXT,
    contenu TEXT NOT NULL,           -- JSON
    empreinte_entree TEXT NOT NULL   -- chaînage : empreinte de l'entrée précédente du fil
);
CREATE TABLE IF NOT EXISTS compteurs (
    jour TEXT PRIMARY KEY,
    appels INTEGER NOT NULL DEFAULT 0,
    executions INTEGER NOT NULL DEFAULT 0,
    jetons INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS etat (
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
"""


class Stockage:
    def __init__(self, chemin: str):
        self._chemin = chemin
        self._verrou = threading.RLock()
        with self._verrou:
            c = self._connexion()
            try:
                c.executescript(_SCHEMA)
            finally:
                c.close()

    def _connexion(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._chemin, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def executer(self, requete: str, parametres: tuple = ()) -> list[sqlite3.Row]:
        with self._verrou:
            c = self._connexion()
            try:
                return c.execute(requete, parametres).fetchall()
            finally:
                c.close()

    def transaction(self, fonction: Callable[[sqlite3.Connection], object]) -> object:
        with self._verrou:
            c = self._connexion()
            try:
                c.execute("BEGIN IMMEDIATE")
                resultat = fonction(c)
                c.execute("COMMIT")
                return resultat
            except Exception:
                c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    def journaliser(self, fil: str, auteur: str, contenu: dict, tache: str | None = None) -> None:
        def _ecrire(c: sqlite3.Connection) -> None:
            precedent = c.execute(
                "SELECT horodatage, auteur, contenu, empreinte_entree FROM journal "
                "WHERE fil=? ORDER BY numero DESC LIMIT 1",
                (fil,),
            ).fetchone()
            base = "" if precedent is None else "|".join(
                [precedent["empreinte_entree"], precedent["horodatage"], precedent["auteur"], precedent["contenu"]]
            )
            c.execute(
                "INSERT INTO journal (fil, horodatage, auteur, tache, contenu, empreinte_entree) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fil, maintenant(), auteur, tache, json.dumps(contenu, ensure_ascii=False),
                 hashlib.sha256(base.encode()).hexdigest()),
            )

        self.transaction(_ecrire)


# ---------------------------------------------------------------------------
# Relais
# ---------------------------------------------------------------------------


@dataclass
class _Tache:
    identifiant: str
    fini: threading.Event = field(default_factory=threading.Event)
    minuteur: threading.Timer | None = None


def _somme(c: sqlite3.Connection, requete: str, parametres: tuple = ()) -> int:
    return int(c.execute(requete, parametres).fetchone()[0] or 0)


class Relais:
    def __init__(
        self,
        config: Config,
        adaptateur: AdaptateurAgent,
        telecharger: Callable[[Config, str, str, str], bytes] = telecharger_depuis_github,
    ):
        self.config = config
        self.adaptateur = adaptateur
        self.telecharger = telecharger
        self.stockage = Stockage(config.chemin_base)
        self._taches: dict[str, _Tache] = {}
        self._reprendre_apres_redemarrage()

    def _reprendre_apres_redemarrage(self) -> None:
        """Une tâche « en_cours » au démarrage a été coupée : sa consommation réelle est inconnue,
        sa réservation est donc comptée comme consommée (hypothèse prudente)."""
        for ligne in self.stockage.executer("SELECT tache, fil FROM taches WHERE ajuste=0"):
            self.stockage.executer(
                "UPDATE taches SET statut='interrompue', fin=? WHERE tache=? AND statut='en_cours'",
                (maintenant(), ligne["tache"]),
            )
            self._isoler_conteneur(ligne["fil"], ligne["tache"], "redemarrage_du_serveur")
        for ligne in self.stockage.executer("SELECT tache FROM taches WHERE ajuste=0"):
            self._ajuster(ligne["tache"], jetons=None, executions=None)

    # -- budget : réservation puis ajustement ------------------------------------

    def _lire_etat(self, c: sqlite3.Connection, cle: str, defaut: int = 0) -> int:
        ligne = c.execute("SELECT valeur FROM etat WHERE cle=?", (cle,)).fetchone()
        return int(ligne["valeur"]) if ligne else defaut

    def _reserver(self, c: sqlite3.Connection, tache: str, fil: str) -> None:
        """Contrôle toutes les limites en comptant les réservations des tâches non soldées,
        puis inscrit la tâche avec sa réservation. Une seule transaction : deux demandes
        simultanées ne peuvent pas se partager la même marge."""
        cfg = self.config
        if cfg.pause:
            raise ErreurRelais("Relais en pause (PAUSE=1) : aucun appel n'est envoyé.")
        maximum_observe = self._lire_etat(c, "max_jetons_observe_par_appel")
        if maximum_observe > cfg.reserve_jetons_par_appel:
            raise ErreurRelais(
                f"Réserve insuffisante : un appel a consommé {maximum_observe} jetons, plus que la "
                f"réserve de {cfg.reserve_jetons_par_appel}. Le plafond n'est plus garanti tant que "
                "RESERVE_JETONS_PAR_APPEL n'est pas relevée au-dessus de cette valeur."
            )
        jour = jour_utc()
        c.execute("INSERT OR IGNORE INTO compteurs (jour) VALUES (?)", (jour,))
        compteur = c.execute("SELECT * FROM compteurs WHERE jour=?", (jour,)).fetchone()

        if compteur["appels"] >= cfg.max_appels_jour:
            raise ErreurRelais(f"Limite d'appels du jour atteinte ({cfg.max_appels_jour}).")

        consommes = _somme(c, "SELECT SUM(jetons) FROM compteurs")
        reserves = _somme(c, "SELECT SUM(jetons_reserves) FROM taches WHERE ajuste=0")
        if consommes + reserves + cfg.reserve_jetons_par_appel > cfg.max_jetons_total:
            raise ErreurRelais(
                f"Plafond de jetons : {consommes} consommés + {reserves} réservés par des tâches en cours "
                f"+ {cfg.reserve_jetons_par_appel} pour cet appel > {cfg.max_jetons_total}."
            )

        executions_reservees = _somme(
            c, "SELECT SUM(executions_reservees) FROM taches WHERE ajuste=0 AND jour=?", (jour,)
        )
        if compteur["executions"] + executions_reservees + cfg.max_appels_outil_par_reponse > cfg.max_executions_jour:
            raise ErreurRelais(
                f"Limite d'exécutions du jour : {compteur['executions']} effectuées + {executions_reservees} "
                f"réservées + {cfg.max_appels_outil_par_reponse} pour cet appel > {cfg.max_executions_jour}."
            )

        c.execute("UPDATE compteurs SET appels = appels + 1 WHERE jour=?", (jour,))
        debut = datetime.now(timezone.utc)
        echeance = datetime.fromtimestamp(debut.timestamp() + cfg.delai_max_tache, timezone.utc)
        c.execute(
            "INSERT INTO taches (tache, fil, statut, debut, echeance, jour, jetons_reserves, executions_reservees) "
            "VALUES (?, ?, 'en_cours', ?, ?, ?, ?, ?)",
            (tache, fil, debut.isoformat(timespec="milliseconds"), echeance.isoformat(timespec="milliseconds"),
             jour, cfg.reserve_jetons_par_appel, cfg.max_appels_outil_par_reponse),
        )

    def _ajuster(self, tache: str, jetons: int | None, executions: int | None) -> dict:
        """Remplace la réservation par la consommation réelle (une seule fois par tâche).

        jetons=None : consommation inconnue (échéance, panne après envoi, redémarrage) :
        la réservation entière est comptée comme consommée.
        """

        def _solder(c: sqlite3.Connection) -> dict:
            t = c.execute("SELECT * FROM taches WHERE tache=?", (tache,)).fetchone()
            if t is None or t["ajuste"]:
                return {}
            jetons_comptes = t["jetons_reserves"] if jetons is None else jetons
            executions_comptees = t["executions_reservees"] if executions is None else executions
            c.execute("INSERT OR IGNORE INTO compteurs (jour) VALUES (?)", (t["jour"],))
            c.execute(
                "UPDATE compteurs SET jetons = jetons + ?, executions = executions + ? WHERE jour=?",
                (jetons_comptes, executions_comptees, t["jour"]),
            )
            c.execute(
                "UPDATE taches SET ajuste=1, jetons_reels=? WHERE tache=?", (jetons_comptes, tache)
            )
            bilan = {
                "jetons_reserves": t["jetons_reserves"],
                "jetons_comptes": jetons_comptes,
                "consommation_connue": jetons is not None,
                "reserve_depassee": jetons_comptes > t["jetons_reserves"],
            }
            if jetons is not None and jetons > self._lire_etat(c, "max_jetons_observe_par_appel"):
                c.execute(
                    "INSERT INTO etat (cle, valeur) VALUES ('max_jetons_observe_par_appel', ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (str(jetons),),
                )
            return bilan

        return self.stockage.transaction(_solder)

    def etat_quotas(self) -> dict:
        cfg = self.config
        jour = jour_utc()

        def _lire(c: sqlite3.Connection) -> dict:
            compteur = c.execute("SELECT * FROM compteurs WHERE jour=?", (jour,)).fetchone()
            consommes = _somme(c, "SELECT SUM(jetons) FROM compteurs")
            reserves = _somme(c, "SELECT SUM(jetons_reserves) FROM taches WHERE ajuste=0")
            maximum = self._lire_etat(c, "max_jetons_observe_par_appel")
            return {
                "jour_utc": jour,
                "pause": cfg.pause,
                "appels": {"utilises": compteur["appels"] if compteur else 0, "limite": cfg.max_appels_jour},
                "executions": {
                    "effectuees": compteur["executions"] if compteur else 0,
                    "reservees": _somme(
                        c, "SELECT SUM(executions_reservees) FROM taches WHERE ajuste=0 AND jour=?", (jour,)
                    ),
                    "limite": cfg.max_executions_jour,
                },
                "jetons": {
                    "consommes": consommes,
                    "reserves_en_cours": reserves,
                    "plafond": cfg.max_jetons_total,
                    "reserve_par_appel": cfg.reserve_jetons_par_appel,
                    "maximum_observe_par_appel": maximum,
                    "plafond_depasse": consommes > cfg.max_jetons_total,
                    "reserve_suffisante": maximum <= cfg.reserve_jetons_par_appel,
                },
                "par_appel": {
                    "max_jetons_sortie": cfg.max_jetons_sortie_par_appel,
                    "max_appels_outil": cfg.max_appels_outil_par_reponse,
                },
                "delais": {"echeance_tache_s": cfg.delai_max_tache, "execution_fichier_s": cfg.delai_execution_fichier},
            }

        return self.stockage.transaction(_lire)

    # -- échéance ----------------------------------------------------------------

    def _expirer(self, tache: str, motif: str = "echeance_depassee") -> bool:
        """Passe la tâche en « expiree » si elle est encore en cours. Renvoie True si c'est fait ici."""

        def _maj(c: sqlite3.Connection) -> tuple[bool, str | None]:
            cur = c.execute(
                "UPDATE taches SET statut='expiree', fin=? WHERE tache=? AND statut='en_cours'",
                (maintenant(), tache),
            )
            fil = c.execute("SELECT fil FROM taches WHERE tache=?", (tache,)).fetchone()
            return cur.rowcount == 1, (fil["fil"] if fil else None)

        fait, fil = self.stockage.transaction(_maj)
        if fait and fil:
            self.stockage.journaliser(
                fil, "relais",
                {"evenement": motif, "delai_max_secondes": self.config.delai_max_tache,
                 "consigne": "Aucun résultat de cette tâche ne sera présenté comme terminé."},
                tache,
            )
        etat = self._taches.get(tache)
        if fait and etat:
            etat.fini.set()  # libère une attente éventuelle de suivre_tache
        return fait

    def _isoler_conteneur(self, fil: str, tache: str, motif: str) -> None:
        """Abandonne le conteneur du fil : le prochain message en créera un nouveau.

        Appliqué dès qu'un appel envoyé ne s'est pas terminé normalement dans les délais
        sans que son arrêt côté fournisseur soit confirmé : l'ancienne exécution pourrait
        encore utiliser ce conteneur.
        """
        lignes = self.stockage.executer("SELECT conteneur FROM fils WHERE fil=?", (fil,))
        if lignes and lignes[0]["conteneur"]:
            self.stockage.executer("UPDATE fils SET conteneur=NULL WHERE fil=?", (fil,))
            self.stockage.journaliser(
                fil, "relais",
                {"evenement": "conteneur_abandonne", "conteneur": lignes[0]["conteneur"], "motif": motif,
                 "consequence": "Le prochain message utilisera un nouveau conteneur isolé."},
                tache,
            )

    # -- envoi d'un message ------------------------------------------------------

    def envoyer_message(
        self,
        fil: str,
        message: str,
        depot: str | None = None,
        chemin: str | None = None,
        commit: str | None = None,
    ) -> dict:
        cfg = self.config
        if not cfg.modele:
            raise ErreurRelais("OPENAI_MODELE n'est pas configuré sur le serveur.")
        if not _RE_FIL.match(fil):
            raise ErreurRelais("Identifiant de fil invalide (lettres, chiffres, . _ -, 64 caractères max).")
        if not message.strip():
            raise ErreurRelais("Message vide.")
        if len(message) > cfg.taille_max_message:
            raise ErreurRelais(f"Message trop long (limite : {cfg.taille_max_message} caractères).")
        references = [depot, chemin, commit]
        if any(references) and not all(references):
            raise ErreurRelais("Pour joindre un fichier, fournir ensemble depot, chemin et commit.")
        if depot:
            valider_reference(cfg, depot, chemin, commit)

        tache = uuid.uuid4().hex[:16]

        def _ouvrir(c: sqlite3.Connection) -> None:
            # Le fil reste occupé tant que l'appel précédent n'est pas effectivement fini
            # (ajuste=0), même si la tâche a déjà été déclarée « expiree » : sinon deux appels
            # pourraient utiliser le même conteneur en même temps.
            occupe = c.execute(
                "SELECT tache, statut FROM taches WHERE fil=? AND "
                "(statut='en_cours' OR (statut='expiree' AND ajuste=0))",
                (fil,),
            ).fetchone()
            if occupe:
                if occupe["statut"] == "expiree":
                    raise ErreurRelais(
                        f"Le fil {fil} reste occupé : la tâche {occupe['tache']} a expiré mais son appel "
                        "n'est pas encore terminé. Utiliser suivre_tache (champ appel_termine) avant de reprendre."
                    )
                raise ErreurRelais(
                    f"Le fil {fil} a déjà une tâche en cours ({occupe['tache']}). "
                    "Utiliser suivre_tache avant d'envoyer un nouveau message."
                )
            self._reserver(c, tache, fil)
            c.execute("INSERT OR IGNORE INTO fils (fil, cree_le) VALUES (?, ?)", (fil, maintenant()))

        self.stockage.transaction(_ouvrir)

        reference = {"depot": depot, "chemin": chemin, "commit": commit} if depot else None
        self.stockage.journaliser(fil, "claude", {"message": message, "fichier": reference}, tache)

        etat = _Tache(tache)
        etat.minuteur = threading.Timer(cfg.delai_max_tache, self._expirer, args=(tache,))
        etat.minuteur.daemon = True
        self._taches[tache] = etat
        etat.minuteur.start()
        threading.Thread(
            target=self._executer, args=(tache, fil, message, reference), daemon=True
        ).start()
        etat.fini.wait(timeout=cfg.attente_synchrone)
        return self.suivre_tache(tache)

    def _executer(self, tache: str, fil: str, message: str, reference: dict | None) -> None:
        cfg = self.config
        debut = time.monotonic()
        appel_lance = False
        jetons = executions = None
        try:
            ligne_fil = self.stockage.executer("SELECT * FROM fils WHERE fil=?", (fil,))[0]
            conteneur = ligne_fil["conteneur"]
            texte_envoye = message
            controle = None

            if reference:
                contenu = self.telecharger(cfg, reference["depot"], reference["chemin"], reference["commit"])
                empreinte_serveur = empreinte(contenu)
                nom = f"{reference['commit'][:12]}_{reference['chemin'].rsplit('/', 1)[-1]}"
                conteneur, chemin_conteneur = self.adaptateur.preparer_conteneur(conteneur, nom, contenu)
                jeton = uuid.uuid4().hex
                bloc = bloc_verification(chemin_conteneur, jeton, cfg.delai_execution_fichier)
                controle = (empreinte_serveur, chemin_conteneur, jeton, bloc)
                texte_envoye = (
                    "[Fichier joint par le relais]\n"
                    f"Dépôt : {reference['depot']}\nChemin : {reference['chemin']}\n"
                    f"Commit : {reference['commit']}\nEmpreinte SHA-256 calculée par le relais : {empreinte_serveur}\n"
                    f"Chemin dans ton conteneur : {chemin_conteneur}\n\n"
                    "Avant toute analyse, exécute EXACTEMENT ce bloc, en un seul appel de l'outil python :\n"
                    f"```python\n{bloc}```\n\n"
                    f"[Message de Claude]\n{message}"
                )

            restant = cfg.delai_max_tache - (time.monotonic() - debut)
            if restant <= 0:
                raise DelaiDepasse(None, False, "échéance atteinte avant l'envoi à l'agent")
            appel_lance = True
            resultat_agent = self.adaptateur.repondre(
                instructions=INSTRUCTIONS_AGENT,
                message=texte_envoye,
                conteneur=conteneur,
                reponse_precedente=ligne_fil["reponse_precedente"],
                max_jetons_sortie=cfg.max_jetons_sortie_par_appel,
                max_appels_outil=cfg.max_appels_outil_par_reponse,
                delai_secondes=restant,
            )
            jetons = (resultat_agent.jetons_entree + resultat_agent.jetons_sortie
                      if resultat_agent.usage_connue else None)
            executions = len(resultat_agent.appels_code)

            preuve = (
                verifier_preuve(resultat_agent.appels_code, *controle)
                if controle
                else {"statut_global": "sans_fichier", "nb_appels_outil": executions}
            )
            resultat = {
                "reponse_agent": resultat_agent.texte,
                "statut_reponse_openai": resultat_agent.statut,
                "identifiant_reponse": resultat_agent.identifiant_reponse,
                "preuve_execution": preuve,
                "traces_execution": [
                    {"code": a.code[: cfg.taille_max_trace],
                     "sorties": "\n".join(a.sorties)[: cfg.taille_max_trace],
                     "conteneur": a.conteneur}
                    for a in resultat_agent.appels_code
                ],
                "jetons": {"entree": resultat_agent.jetons_entree, "sortie": resultat_agent.jetons_sortie},
                "duree_secondes": round(time.monotonic() - debut, 2),
            }

            # Clôture dans les délais seulement : condition sur le statut ET sur la durée.
            dans_les_delais = (time.monotonic() - debut) <= cfg.delai_max_tache
            if not dans_les_delais:
                self._expirer(tache)

            def _clore(c: sqlite3.Connection) -> bool:
                if not dans_les_delais:
                    c.execute(
                        "UPDATE taches SET resultat_tardif=? WHERE tache=?",
                        (json.dumps(resultat, ensure_ascii=False), tache),
                    )
                    return False
                cur = c.execute(
                    "UPDATE taches SET statut='terminee', fin=?, resultat=? WHERE tache=? AND statut='en_cours'",
                    (maintenant(), json.dumps(resultat, ensure_ascii=False), tache),
                )
                if cur.rowcount == 0:  # le minuteur a expiré la tâche entre-temps
                    c.execute(
                        "UPDATE taches SET resultat_tardif=? WHERE tache=?",
                        (json.dumps(resultat, ensure_ascii=False), tache),
                    )
                    return False
                c.execute(
                    "UPDATE fils SET reponse_precedente=?, conteneur=? WHERE fil=?",
                    (resultat_agent.identifiant_reponse, conteneur, fil),
                )
                return True

            if self.stockage.transaction(_clore):
                self.stockage.journaliser(fil, "agent", resultat, tache)
            else:
                self._isoler_conteneur(fil, tache, "reponse_tardive")
                # Réponse tardive : conservée pour examen, jamais chaînée à l'historique du fil.
                self.stockage.journaliser(
                    fil, "relais",
                    {"evenement": "reponse_tardive_ignoree",
                     "duree_secondes": resultat["duree_secondes"],
                     "resultat_tardif": resultat},
                    tache,
                )
        except DelaiDepasse as exc:
            self._expirer(tache)
            self.stockage.journaliser(
                fil, "relais",
                {"evenement": "reponse_abandonnee_a_echeance",
                 "identifiant_reponse": exc.identifiant_reponse,
                 "annulation_demandee": exc.annulation_demandee,
                 "arret_confirme": exc.arret_confirme,
                 "detail": exc.detail,
                 "facturation": "Une annulation ne garantit pas l'arrêt de la facturation déjà engagée ; "
                                "la réservation entière est comptée comme consommée."},
                tache,
            )
            if appel_lance and not exc.arret_confirme:
                self._isoler_conteneur(fil, tache, "arret_non_confirme_a_echeance")
        except Exception as exc:  # toute erreur est consignée, jamais avalée
            erreur = {"erreur": f"{type(exc).__name__}: {exc}", "appel_api_lance": appel_lance}

            def _echouer(c: sqlite3.Connection) -> None:
                c.execute(
                    "UPDATE taches SET statut='echec', fin=?, resultat=? WHERE tache=? AND statut='en_cours'",
                    (maintenant(), json.dumps(erreur, ensure_ascii=False), tache),
                )

            self.stockage.transaction(_echouer)
            self.stockage.journaliser(fil, "relais", erreur, tache)
            if appel_lance:  # l'appel distant a pu continuer malgré l'erreur locale
                self._isoler_conteneur(fil, tache, "erreur_apres_envoi")
        finally:
            if not appel_lance:  # rien n'a été envoyé à l'agent : aucune consommation
                jetons, executions = 0, 0
            bilan = self._ajuster(tache, jetons, executions)
            if bilan.get("reserve_depassee"):
                self.stockage.journaliser(
                    fil, "relais",
                    {"evenement": "reserve_depassee", **bilan,
                     "consequence": "Nouveaux appels refusés jusqu'à relèvement de RESERVE_JETONS_PAR_APPEL."},
                    tache,
                )
            etat = self._taches.pop(tache, None)
            if etat:
                if etat.minuteur:
                    etat.minuteur.cancel()
                etat.fini.set()

    # -- suivi -------------------------------------------------------------------

    def suivre_tache(self, tache: str, attente_secondes: float = 0.0) -> dict:
        etat = self._taches.get(tache)
        if etat and attente_secondes > 0:
            etat.fini.wait(timeout=min(attente_secondes, self.config.attente_synchrone))
        lignes = self.stockage.executer("SELECT * FROM taches WHERE tache=?", (tache,))
        if not lignes:
            raise ErreurRelais(f"Tâche inconnue : {tache}.")
        ligne = lignes[0]
        if ligne["statut"] == "en_cours" and datetime.now(timezone.utc) > datetime.fromisoformat(ligne["echeance"]):
            self._expirer(tache)  # filet de sécurité si le minuteur n'a pas encore agi
            ligne = self.stockage.executer("SELECT * FROM taches WHERE tache=?", (tache,))[0]

        reponse = {
            "tache": tache,
            "fil": ligne["fil"],
            "statut": ligne["statut"],
            "debut": ligne["debut"],
            "echeance": ligne["echeance"],
            "fin": ligne["fin"],
            "appel_termine": bool(ligne["ajuste"]),
        }
        if ligne["statut"] == "en_cours":
            ecoule = (datetime.now(timezone.utc) - datetime.fromisoformat(ligne["debut"])).total_seconds()
            reponse["ecoule_secondes"] = round(ecoule, 1)
            reponse["consigne"] = "Rappeler suivre_tache avec cet identifiant."
        elif ligne["statut"] == "terminee":
            reponse["resultat"] = json.loads(ligne["resultat"])
        elif ligne["statut"] == "echec":
            reponse["resultat"] = json.loads(ligne["resultat"]) if ligne["resultat"] else None
        elif ligne["statut"] == "expiree":
            reponse["consigne"] = (
                "Échéance dépassée : la tâche n'a pas abouti dans les délais. "
                "Un résultat arrivé après l'échéance est disponible pour examen seulement. "
                + ("Le fil est libre ; le prochain message utilisera un nouveau conteneur."
                   if ligne["ajuste"] else
                   "L'appel n'est pas encore terminé : le fil reste occupé jusqu'à sa fin.")
            )
            if ligne["resultat_tardif"]:
                reponse["resultat_tardif_hors_delai"] = json.loads(ligne["resultat_tardif"])
        return reponse

    # -- journal -----------------------------------------------------------------

    def lire_journal(self, fil: str, depuis: int = 0) -> dict:
        lignes = self.stockage.executer(
            "SELECT * FROM journal WHERE fil=? AND numero > ? ORDER BY numero", (fil, depuis)
        )
        return {
            "fil": fil,
            "entrees": [
                {
                    "numero": l["numero"],
                    "horodatage": l["horodatage"],
                    "auteur": l["auteur"],
                    "tache": l["tache"],
                    "contenu": json.loads(l["contenu"]),
                    "empreinte_chainee": l["empreinte_entree"],
                }
                for l in lignes
            ],
        }


__all__ = [
    "AppelCode",
    "ResultatAgent",
    "AdaptateurAgent",
    "DelaiDepasse",
    "ErreurRelais",
    "Relais",
    "bloc_verification",
    "verifier_preuve",
    "empreinte",
]
