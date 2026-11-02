"""Agents simulés pour les tests : ils remplacent OpenAI sans réseau.

AgentExecutant exécute réellement le code reçu (dans un répertoire temporaire jouant
le rôle de conteneur) : la chaîne preuve -> vérification est testée sur de vraies
sorties, pas sur des chaînes fabriquées à la main. Les variantes reproduisent les
comportements qu'un vrai agent pourrait avoir : ne faire qu'une partie du travail,
modifier le bloc, rejouer une ancienne trace, dépasser l'échéance.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from relais import AppelCode, DelaiDepasse, ResultatAgent

_RE_BLOC = re.compile(r"```python\n(.*?)```", re.DOTALL)
_RE_CHEMIN = re.compile(r"^CHEMIN = '(.*)'$", re.MULTILINE)


def executer(code: str) -> str:
    sortie = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return sortie.stdout + sortie.stderr


class AgentExecutant:
    """Agent coopératif : exécute exactement le bloc fourni."""

    def __init__(self, delai: float = 0.0, respecte_echeance: bool = False,
                 falsifier_fichier: bool = False, jetons: tuple[int, int] = (1000, 200),
                 arret_confirme: bool = False):
        self.delai = delai
        self.respecte_echeance = respecte_echeance
        self.arret_confirme = arret_confirme
        self.en_cours_par_conteneur: dict = {}
        self.simultanes_max_par_conteneur = 0
        self.falsifier_fichier = falsifier_fichier
        self.jetons = jetons
        self.appels: list[dict] = []
        self._dossiers: dict[str, Path] = {}
        self._verrou = threading.Lock()

    def preparer_conteneur(self, conteneur, nom_fichier, contenu):
        with self._verrou:
            if conteneur not in self._dossiers:
                conteneur = "cntr_" + uuid.uuid4().hex[:8]
                self._dossiers[conteneur] = Path(tempfile.mkdtemp(prefix="conteneur_"))
            chemin = self._dossiers[conteneur] / nom_fichier
            if self.falsifier_fichier:  # fichier modifié entre le relais et l'exécution
                contenu = contenu + b"\n# modification\n"
            chemin.write_bytes(contenu)
            return conteneur, str(chemin)

    def code_a_executer(self, bloc: str) -> list[str]:
        return [bloc]

    def repondre(self, *, conteneur, **kwargs):
        """Mesure le nombre d'appels simultanés sur un même conteneur."""
        with self._verrou:
            n = self.en_cours_par_conteneur.get(conteneur, 0) + 1
            self.en_cours_par_conteneur[conteneur] = n
            self.simultanes_max_par_conteneur = max(self.simultanes_max_par_conteneur, n)
        try:
            return self._repondre(conteneur=conteneur, **kwargs)
        finally:
            with self._verrou:
                self.en_cours_par_conteneur[conteneur] -= 1

    def _repondre(self, *, instructions, message, conteneur, reponse_precedente,
                  max_jetons_sortie, max_appels_outil, delai_secondes):
        with self._verrou:
            numero = len(self.appels) + 1
            self.appels.append(
                {"message": message, "reponse_precedente": reponse_precedente, "conteneur": conteneur,
                 "max_jetons_sortie": max_jetons_sortie, "max_appels_outil": max_appels_outil,
                 "delai_secondes": delai_secondes}
            )
        if self.delai:
            if self.respecte_echeance and self.delai > delai_secondes:
                time.sleep(delai_secondes)
                raise DelaiDepasse(f"resp_{numero}", True, "simulation : annulation à l'échéance",
                                   arret_confirme=self.arret_confirme)
            time.sleep(self.delai)
        appels_code = []
        bloc = _RE_BLOC.search(message)
        if bloc:
            for code in self.code_a_executer(bloc.group(1)):
                appels_code.append(AppelCode(code=code, sorties=[executer(code)], conteneur=conteneur))
        return ResultatAgent(
            identifiant_reponse=f"resp_{numero}",
            texte=f"Réponse simulée n°{numero} (précédente : {reponse_precedente}).",
            appels_code=appels_code,
            jetons_entree=self.jetons[0],
            jetons_sortie=self.jetons[1],
        )


class AgentMenteur(AgentExecutant):
    """Affirme avoir exécuté, sans aucun appel à l'outil d'exécution."""

    def code_a_executer(self, bloc):
        return []

    def _repondre(self, **kwargs):
        resultat = super()._repondre(**kwargs)
        resultat.texte = "J'ai exécuté le fichier, tout est correct. EMPREINTE_CONTENEUR=0000"
        return resultat


class AgentLecteurSeul(AgentExecutant):
    """Lit le fichier et affiche son empreinte, mais ne l'exécute pas."""

    def code_a_executer(self, bloc):
        chemin = _RE_CHEMIN.search(bloc).group(1)
        return [
            "import hashlib\n"
            f"CHEMIN = {chemin!r}\n"
            "print('EMPREINTE_CONTENEUR=' + hashlib.sha256(open(CHEMIN, 'rb').read()).hexdigest())\n"
        ]


class AgentFaussaire(AgentExecutant):
    """Exécute un bloc modifié qui imite toutes les lignes de contrôle sans lancer le fichier."""

    def code_a_executer(self, bloc):
        return [bloc.replace(
            "r = subprocess.run([sys.executable, CHEMIN]",
            "r = subprocess.run([sys.executable, '-c', 'print(1)']",
        )]


class AgentPremierePartieSeulement(AgentExecutant):
    """Exécute le bloc conforme mais la trace s'arrête avant le résultat (sortie tronquée)."""

    def _repondre(self, **kwargs):
        resultat = super()._repondre(**kwargs)
        for appel in resultat.appels_code:
            appel.sorties = ["\n".join(appel.sorties[0].splitlines()[:2])]
        return resultat


class AgentRejoueur(AgentExecutant):
    """Renvoie, pour chaque tâche, la trace d'exécution de la toute première tâche."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.premiere_trace = None

    def _repondre(self, **kwargs):
        resultat = super()._repondre(**kwargs)
        if self.premiere_trace is None:
            self.premiere_trace = resultat.appels_code
        else:
            resultat.appels_code = self.premiere_trace
        return resultat


class AgentSansUsage(AgentExecutant):
    def _repondre(self, **kwargs):
        resultat = super()._repondre(**kwargs)
        resultat.usage_connue = False
        resultat.jetons_entree = resultat.jetons_sortie = 0
        return resultat


class AgentEnPanne(AgentExecutant):
    def _repondre(self, **kwargs):
        self.appels.append(kwargs)
        raise RuntimeError("API indisponible (simulation)")
