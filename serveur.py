"""Serveur MCP distant exposant le relais à Claude (connecteur personnalisé).

Lancement :
    uvicorn serveur:creer_application_depuis_environnement --factory --host 0.0.0.0 --port 8000

Point d'accès MCP : /mcp   (transport HTTP « streamable », sans état de session)
Contrôle de santé : /sante (sans authentification, ne révèle rien)

Authentification : chaque requête doit porter l'en-tête
    Authorization: Bearer <MCP_SECRET>
Solution de repli, si l'interface du connecteur ne permet pas d'en-tête fixe :
    URL https://<hote>/s/<MCP_SECRET>/mcp
(moins sûre : le secret peut apparaître dans les journaux d'accès de l'hébergeur).
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from config import Config
from relais import AdaptateurAgent, ErreurRelais, Relais, telecharger_depuis_github

journal_serveur = logging.getLogger("relais")


# ---------------------------------------------------------------------------
# Authentification (middleware ASGI)
# ---------------------------------------------------------------------------


class AuthentificationBearer:
    def __init__(self, application, secret: str):
        self.application = application
        self._secret = secret.encode()

    async def _repondre(self, send, statut: int, corps: dict) -> None:
        donnees = json.dumps(corps).encode()
        entetes = [(b"content-type", b"application/json"), (b"content-length", str(len(donnees)).encode())]
        if statut == 401:
            entetes.append((b"www-authenticate", b'Bearer realm="relais"'))
        await send({"type": "http.response.start", "status": statut, "headers": entetes})
        await send({"type": "http.response.body", "body": donnees})

    def _secret_valide(self, candidat: bytes) -> bool:
        return hmac.compare_digest(candidat, self._secret)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan, etc.
            return await self.application(scope, receive, send)

        chemin: str = scope.get("path", "")
        if chemin == "/sante":
            return await self._repondre(send, 200, {"statut": "ok"})

        # Variante de repli : secret dans le chemin /s/<secret>/...
        if chemin.startswith("/s/"):
            morceaux = chemin.split("/", 3)  # ['', 's', '<secret>', 'reste']
            if len(morceaux) == 4 and self._secret_valide(morceaux[2].encode()):
                scope = dict(scope)
                scope["path"] = "/" + morceaux[3]
                scope["raw_path"] = scope["path"].encode()
                return await self.application(scope, receive, send)
            return await self._repondre(send, 401, {"erreur": "non autorisé"})

        entete = dict(scope.get("headers") or []).get(b"authorization", b"")
        prefixe = b"bearer "
        if entete[: len(prefixe)].lower() == prefixe and self._secret_valide(entete[len(prefixe):].strip()):
            return await self.application(scope, receive, send)
        return await self._repondre(send, 401, {"erreur": "non autorisé"})


# ---------------------------------------------------------------------------
# Outils MCP
# ---------------------------------------------------------------------------

DESCRIPTION_SERVEUR = (
    "Relais vers un agent OpenAI chargé de la revue critique et de la validation. "
    "L'agent dispose d'un outil d'exécution Python (conteneur isolé). "
    "Chaque fil conserve son historique ; un seul message à la fois par fil."
)


def _protege(fonction: Callable[..., dict]) -> Callable[..., Any]:
    """Transforme les refus métier en réponse structurée plutôt qu'en exception opaque."""

    async def enveloppe(*args, **kwargs) -> dict:
        try:
            return await anyio.to_thread.run_sync(lambda: fonction(*args, **kwargs))
        except ErreurRelais as exc:
            return {"statut": "refuse", "raison": str(exc)}

    return enveloppe


def construire_serveur_mcp(relais: Relais) -> MCPServer:
    serveur = MCPServer(name="relais-openai", instructions=DESCRIPTION_SERVEUR)

    @serveur.tool(
        name="envoyer_message",
        description=(
            "Envoie un message à l'agent OpenAI dans le fil indiqué, avec éventuellement un fichier "
            "pris sur GitHub à un commit précis (SHA complet de 40 caractères). Si un fichier est joint, "
            "l'agent l'exécute et le relais vérifie l'empreinte SHA-256 du fichier réellement exécuté "
            "à partir des traces de l'outil. preuve_execution distingue trois constats : fichier "
            "(verifie / discordant / non_lu), execution (effectuee / incomplete / non_conforme / absente) "
            "et reussite (reussie / echouee / delai_depasse / inconnue) ; statut_global = verifiee seulement "
            "si le fichier est vérifié et le bloc de contrôle exécuté jusqu'au bout. "
            "Si l'exécution dépasse l'attente synchrone, le statut est 'en_cours' : appeler suivre_tache."
        ),
    )
    async def envoyer_message(
        fil: str,
        message: str,
        depot: str | None = None,
        chemin: str | None = None,
        commit: str | None = None,
    ) -> dict:
        return await _protege(relais.envoyer_message)(fil, message, depot, chemin, commit)

    @serveur.tool(
        name="suivre_tache",
        description=(
            "Renvoie l'état d'une tâche (en_cours, terminee, echec, expiree, interrompue) et son résultat "
            "si elle s'est terminée dans les délais. Une tâche expiree n'a pas abouti dans les délais : un "
            "éventuel résultat tardif est fourni à part, pour examen seulement. attente_secondes permet d'attendre la fin, dans la limite de l'attente synchrone."
        ),
    )
    async def suivre_tache(tache: str, attente_secondes: float = 0.0) -> dict:
        return await _protege(relais.suivre_tache)(tache, attente_secondes)

    @serveur.tool(
        name="lire_journal",
        description=(
            "Renvoie le journal horodaté d'un fil : messages de Claude, réponses de l'agent avec preuves "
            "et traces d'exécution, erreurs du relais. 'depuis' filtre les entrées de numéro supérieur."
        ),
    )
    async def lire_journal(fil: str, depuis: int = 0) -> dict:
        return await _protege(relais.lire_journal)(fil, depuis)

    @serveur.tool(
        name="etat_quotas",
        description="Renvoie la consommation du jour et les limites de dépense appliquées par le relais.",
    )
    async def etat_quotas() -> dict:
        return await _protege(relais.etat_quotas)()

    return serveur


# ---------------------------------------------------------------------------
# Assemblage de l'application
# ---------------------------------------------------------------------------


def creer_application(
    config: Config,
    adaptateur: AdaptateurAgent,
    telecharger: Callable[[Config, str, str, str], bytes] = telecharger_depuis_github,
):
    relais = Relais(config, adaptateur, telecharger)
    serveur = construire_serveur_mcp(relais)
    if config.hotes_autorises:
        securite = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.hotes_autorises,
            allowed_origins=[f"https://{h}" for h in config.hotes_autorises],
        )
    else:
        securite = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    application = serveur.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=securite,
    )
    return AuthentificationBearer(application, config.secret_mcp)


def creer_application_depuis_environnement():
    logging.basicConfig(level=logging.INFO)
    config = Config.depuis_environnement()
    if not config.modele:
        raise RuntimeError("OPENAI_MODELE doit être renseigné.")
    if not config.depots_autorises:
        raise RuntimeError("DEPOTS_AUTORISES doit lister au moins un dépôt (proprietaire/depot).")
    from adaptateur_openai import AdaptateurOpenAI

    adaptateur = AdaptateurOpenAI(
        config.modele, config.memoire_conteneur, config.delai_max_tache, arriere_plan=config.mode_arriere_plan
    )
    return creer_application(config, adaptateur)
