"""Configuration du relais, lue une seule fois depuis les variables d'environnement.

Toutes les limites de dépense du prototype sont ici. Les valeurs par défaut sont
volontairement basses : il vaut mieux qu'un test soit refusé que de laisser une
boucle consommer un budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _entier(nom: str, defaut: int) -> int:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    try:
        return int(valeur)
    except ValueError as exc:
        raise ValueError(f"{nom} doit être un entier, reçu {valeur!r}") from exc


def _reel(nom: str, defaut: float) -> float:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    try:
        return float(valeur)
    except ValueError as exc:
        raise ValueError(f"{nom} doit être un nombre, reçu {valeur!r}") from exc


def _liste(nom: str) -> list[str]:
    brut = os.environ.get(nom, "")
    return [element.strip() for element in brut.split(",") if element.strip()]


@dataclass(frozen=True)
class Config:
    # Sécurité
    secret_mcp: str
    hotes_autorises: list[str] = field(default_factory=list)

    # Agent OpenAI
    modele: str = ""
    memoire_conteneur: str = "1g"

    # Sources de fichiers
    depots_autorises: list[str] = field(default_factory=list)
    jeton_github: str | None = None
    taille_max_fichier: int = 200_000          # octets
    taille_max_message: int = 20_000           # caractères

    # Limites de dépense (bloquantes, appliquées par le serveur)
    max_appels_jour: int = 20                  # messages envoyés à l'agent par jour (UTC)
    max_executions_jour: int = 20              # appels à l'outil d'exécution par jour (UTC)
    max_jetons_total: int = 300_000            # jetons cumulés (entrée + sortie) depuis l'installation
    max_jetons_sortie_par_appel: int = 4_000
    max_appels_outil_par_reponse: int = 3
    # Jetons réservés AVANT chaque appel. L'API ne permet pas de borner à l'avance les jetons
    # d'entrée d'une réponse à plusieurs étapes d'outil : cette réserve est donc une estimation
    # prudente. Si une consommation réelle la dépasse, le relais se bloque jusqu'à ce qu'elle
    # soit relevée au-dessus du maximum observé.
    reserve_jetons_par_appel: int = 50_000
    pause: bool = False                        # coupe-circuit : refuse tout appel

    # Délais
    attente_synchrone: float = 25.0            # secondes d'attente avant de rendre un identifiant de tâche
    delai_max_tache: float = 600.0             # échéance stricte : au-delà, la tâche est « expiree »
    delai_execution_fichier: float = 120.0     # durée maximale d'exécution du fichier dans le conteneur
    mode_arriere_plan: bool = True             # OpenAI : réponse en arrière-plan, annulable à l'échéance

    # Stockage
    chemin_base: str = "relais.sqlite3"
    taille_max_trace: int = 8_000              # caractères conservés par trace d'exécution

    @staticmethod
    def depuis_environnement() -> "Config":
        secret = os.environ.get("MCP_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError(
                "MCP_SECRET est absent ou trop court (32 caractères minimum). "
                "Générez-le avec : python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return Config(
            secret_mcp=secret,
            hotes_autorises=_liste("MCP_HOTES_AUTORISES"),
            modele=os.environ.get("OPENAI_MODELE", "").strip(),
            memoire_conteneur=os.environ.get("OPENAI_MEMOIRE_CONTENEUR", "1g").strip(),
            depots_autorises=_liste("DEPOTS_AUTORISES"),
            jeton_github=os.environ.get("GITHUB_TOKEN") or None,
            taille_max_fichier=_entier("TAILLE_MAX_FICHIER", 200_000),
            taille_max_message=_entier("TAILLE_MAX_MESSAGE", 20_000),
            max_appels_jour=_entier("MAX_APPELS_JOUR", 20),
            max_executions_jour=_entier("MAX_EXECUTIONS_JOUR", 20),
            max_jetons_total=_entier("MAX_JETONS_TOTAL", 300_000),
            max_jetons_sortie_par_appel=_entier("MAX_JETONS_SORTIE_PAR_APPEL", 4_000),
            max_appels_outil_par_reponse=_entier("MAX_APPELS_OUTIL_PAR_REPONSE", 3),
            reserve_jetons_par_appel=_entier("RESERVE_JETONS_PAR_APPEL", 50_000),
            pause=os.environ.get("PAUSE", "0").strip() in {"1", "true", "oui"},
            attente_synchrone=_reel("ATTENTE_SYNCHRONE", 25.0),
            delai_max_tache=_reel("DELAI_MAX_TACHE", 600.0),
            delai_execution_fichier=_reel("DELAI_EXECUTION_FICHIER", 120.0),
            mode_arriere_plan=os.environ.get("OPENAI_MODE_ARRIERE_PLAN", "1").strip() in {"1", "true", "oui"},
            chemin_base=os.environ.get("CHEMIN_BASE", "relais.sqlite3"),
            taille_max_trace=_entier("TAILLE_MAX_TRACE", 8_000),
        )
