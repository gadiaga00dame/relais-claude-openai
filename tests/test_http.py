"""Test de bout en bout par HTTP réel : serveur uvicorn local + client MCP officiel.

Vérifie l'authentification et le dialogue MCP complet (liste des outils, envoi,
suivi, journal), avec l'agent simulé à la place d'OpenAI.
"""

import json
import socket
import threading
import time
from pathlib import Path

import anyio
import httpx2
import pytest
import requests
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from agents_simules import AgentExecutant
from config import Config
from serveur import creer_application

RACINE = Path(__file__).resolve().parent.parent
FICHIER = (RACINE / "exemple" / "calcul_test.py").read_bytes()
SECRET = "secret-de-test-" + "x" * 30
DEPOT = "dame/relais-test"


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def url_serveur(tmp_path_factory):
    config = Config(
        secret_mcp=SECRET,
        modele="modele-test",
        depots_autorises=[DEPOT],
        chemin_base=str(tmp_path_factory.mktemp("base") / "relais.sqlite3"),
        attente_synchrone=10.0,
    )
    application = creer_application(config, AgentExecutant(), lambda *a: FICHIER)
    port = _port_libre()
    serveur = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning"))
    fil = threading.Thread(target=serveur.run, daemon=True)
    fil.start()
    for _ in range(100):
        if serveur.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    serveur.should_exit = True
    fil.join(timeout=5)


def _sans_proxy():
    return {"http": None, "https": None}


def test_sante_sans_authentification(url_serveur):
    r = requests.get(url_serveur + "/sante", proxies=_sans_proxy())
    assert r.status_code == 200 and r.json() == {"statut": "ok"}


@pytest.mark.parametrize("entete", [None, "Bearer mauvais", "Basic " + SECRET, SECRET])
def test_refus_sans_bon_secret(url_serveur, entete):
    entetes = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if entete:
        entetes["Authorization"] = entete
    r = requests.post(url_serveur + "/mcp", headers=entetes, data="{}", proxies=_sans_proxy())
    assert r.status_code == 401


def test_secret_dans_url_mauvais(url_serveur):
    r = requests.post(url_serveur + "/s/mauvais/mcp", data="{}", proxies=_sans_proxy())
    assert r.status_code == 401


def _contenu(resultat) -> dict:
    if resultat.structured_content is not None:
        sc = resultat.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(resultat.content[0].text)


async def _scenario(url: str, entetes: dict) -> dict:
    client_http = httpx2.AsyncClient(headers=entetes, timeout=60, trust_env=False)
    async with client_http, Client(streamable_http_client(url, http_client=client_http)) as client:
        outils = sorted(t.name for t in (await client.list_tools()).tools)
        r1 = _contenu(await client.call_tool("envoyer_message", {
            "fil": "demo", "message": "Exécute le fichier et commente.",
            "depot": DEPOT, "chemin": "exemple/calcul_test.py", "commit": "b" * 40,
        }))
        r2 = _contenu(await client.call_tool("envoyer_message", {"fil": "demo", "message": "Suite ?"}))
        journal = _contenu(await client.call_tool("lire_journal", {"fil": "demo"}))
        quotas = _contenu(await client.call_tool("etat_quotas", {}))
        refus = _contenu(await client.call_tool("envoyer_message", {
            "fil": "demo", "message": "x", "depot": "autre/depot", "chemin": "a.py", "commit": "b" * 40,
        }))
        return {"outils": outils, "r1": r1, "r2": r2, "journal": journal, "quotas": quotas, "refus": refus}


@pytest.mark.parametrize("mode", ["entete", "url"])
def test_dialogue_mcp_complet(url_serveur, mode):
    if mode == "entete":
        url, entetes = url_serveur + "/mcp", {"Authorization": f"Bearer {SECRET}"}
    else:
        url, entetes = f"{url_serveur}/s/{SECRET}/mcp", {}
    res = anyio.run(_scenario, url, entetes)

    assert res["outils"] == ["envoyer_message", "etat_quotas", "lire_journal", "suivre_tache"]
    assert res["r1"]["statut"] == "terminee"
    assert res["r1"]["resultat"]["preuve_execution"]["statut_global"] == "verifiee"
    assert res["r1"]["resultat"]["preuve_execution"]["reussite"] == "reussie"
    assert res["r2"]["statut"] == "terminee"
    assert "resp_" in res["r2"]["resultat"]["reponse_agent"]
    auteurs = [e["auteur"] for e in res["journal"]["entrees"]]
    assert auteurs[-4:] == ["claude", "agent", "claude", "agent"]
    assert res["quotas"]["appels"]["utilises"] >= 2
    assert res["refus"]["statut"] == "refuse" and "non autorisé" in res["refus"]["raison"]
