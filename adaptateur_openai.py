"""Adaptateur vers l'API OpenAI : API Responses + outil d'exécution Code Interpreter.

C'est la seule partie du relais qui parle à OpenAI. Elle n'a pas été exécutée
contre l'API réelle : le premier appel réel aura lieu au premier essai connecté.
En cas d'écart avec la version installée de la bibliothèque `openai`, c'est ici
qu'il faudra corriger.

Choix techniques :
- conteneur créé explicitement (`containers.create`) puis fichier déposé directement
  dans le conteneur (`containers.files.create`) : on connaît l'identifiant du
  conteneur et le chemin du fichier, ce qui sert à la preuve d'exécution ;
- `include=["code_interpreter_call.outputs"]` pour recevoir les sorties brutes de l'outil ;
- `previous_response_id` pour chaîner l'historique du fil côté OpenAI ;
- `max_output_tokens` et `max_tool_calls` pour borner chaque appel ;
- mode arrière-plan (`background=True`) : la réponse est interrogée jusqu'à l'échéance,
  puis annulée (`responses.cancel`) si elle n'est pas finie. La documentation ne précise
  pas si l'annulation arrête la facturation déjà engagée : le relais compte donc la
  réservation entière comme consommée. La documentation ne confirme pas non plus que
  Code Interpreter fonctionne en arrière-plan : si ce n'est pas le cas, mettre
  OPENAI_MODE_ARRIERE_PLAN=0 (appel synchrone coupé côté client à l'échéance, sans
  annulation possible côté OpenAI).

Un conteneur expire après 20 minutes d'inactivité ; il est alors recréé et le fichier redéposé.
"""

from __future__ import annotations

import time

from relais import AppelCode, DelaiDepasse, ResultatAgent

INCLURE = ["code_interpreter_call.outputs"]
STATUTS_EN_COURS = {"queued", "in_progress"}
STATUTS_EXPLOITABLES = {"completed", "incomplete"}  # incomplete : plafond de sortie atteint


class AdaptateurOpenAI:
    def __init__(
        self,
        modele: str,
        memoire_conteneur: str = "1g",
        delai_secondes: float = 600.0,
        arriere_plan: bool = True,
        intervalle_interrogation: float = 2.0,
    ):
        from openai import OpenAI  # lit OPENAI_API_KEY dans l'environnement

        self.client = OpenAI(timeout=min(delai_secondes, 120.0), max_retries=1)
        self.modele = modele
        self.memoire = memoire_conteneur
        self.arriere_plan = arriere_plan
        self.intervalle = intervalle_interrogation

    # -- conteneur -------------------------------------------------------------

    def _conteneur_actif(self, conteneur: str | None) -> str | None:
        if not conteneur:
            return None
        from openai import NotFoundError

        try:
            info = self.client.containers.retrieve(conteneur)
        except NotFoundError:
            return None
        statut = getattr(info, "status", "")
        return None if statut in {"expired", "deleted"} else conteneur

    def preparer_conteneur(self, conteneur: str | None, nom_fichier: str, contenu: bytes) -> tuple[str, str]:
        conteneur = self._conteneur_actif(conteneur)
        if conteneur is None:
            cree = self.client.containers.create(name="relais-claude", memory_limit=self.memoire)
            conteneur = cree.id
        fichier = self.client.containers.files.create(
            container_id=conteneur, file=(nom_fichier, contenu)
        )
        chemin = getattr(fichier, "path", None) or f"/mnt/data/{nom_fichier}"
        return conteneur, chemin

    # -- réponse ---------------------------------------------------------------

    def _recuperer(self, identifiant: str):
        try:
            return self.client.responses.retrieve(identifiant, include=INCLURE)
        except TypeError:  # version de la bibliothèque sans paramètre include sur retrieve
            return self.client.responses.retrieve(identifiant)

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
        echeance = time.monotonic() + delai_secondes
        outil = {
            "type": "code_interpreter",
            "container": conteneur if conteneur else {"type": "auto", "memory_limit": self.memoire},
        }
        parametres = dict(
            model=self.modele,
            instructions=instructions,
            input=message,
            tools=[outil],
            include=INCLURE,
            max_output_tokens=max_jetons_sortie,
            max_tool_calls=max_appels_outil,
            store=True,  # nécessaire pour chaîner l'historique avec previous_response_id
        )
        if reponse_precedente:
            parametres["previous_response_id"] = reponse_precedente

        if self.arriere_plan:
            reponse = self.client.responses.create(**parametres, background=True)
            while getattr(reponse, "status", None) in STATUTS_EN_COURS:
                if time.monotonic() >= echeance:
                    demandee, confirme = False, False
                    try:
                        finale = self.client.responses.cancel(reponse.id)
                        demandee = True
                        confirme = getattr(finale, "status", None) == "cancelled"
                    except Exception:
                        pass
                    raise DelaiDepasse(reponse.id, demandee, "réponse non terminée à l'échéance",
                                       arret_confirme=confirme)
                time.sleep(min(self.intervalle, max(0.0, echeance - time.monotonic())))
                reponse = self._recuperer(reponse.id)
        else:
            from openai import APITimeoutError

            try:
                reponse = self.client.with_options(timeout=max(1.0, delai_secondes)).responses.create(**parametres)
            except APITimeoutError as exc:
                raise DelaiDepasse(None, False, "délai client atteint ; aucune annulation possible en mode synchrone") from exc

        statut = getattr(reponse, "status", "inconnu") or "inconnu"
        if statut not in STATUTS_EXPLOITABLES:
            raise RuntimeError(f"Réponse OpenAI au statut {statut} : {getattr(reponse, 'error', None)}")

        appels: list[AppelCode] = []
        for element in reponse.output or []:
            if getattr(element, "type", None) != "code_interpreter_call":
                continue
            sorties: list[str] = []
            for sortie in getattr(element, "outputs", None) or []:
                if getattr(sortie, "type", None) == "logs":
                    sorties.append(getattr(sortie, "logs", "") or "")
                else:
                    sorties.append(f"[sortie de type {getattr(sortie, 'type', '?')}]")
            appels.append(
                AppelCode(
                    code=getattr(element, "code", "") or "",
                    sorties=sorties,
                    conteneur=getattr(element, "container_id", None),
                )
            )

        usage = getattr(reponse, "usage", None)
        return ResultatAgent(
            identifiant_reponse=reponse.id,
            texte=getattr(reponse, "output_text", "") or "",
            appels_code=appels,
            jetons_entree=getattr(usage, "input_tokens", 0) or 0,
            jetons_sortie=getattr(usage, "output_tokens", 0) or 0,
            statut=statut,
            usage_connue=usage is not None,
        )
