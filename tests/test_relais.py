"""Tests du cœur du relais, avec agents simulés (aucun appel réseau)."""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agents_simules import (
    AgentEnPanne,
    AgentExecutant,
    AgentFaussaire,
    AgentLecteurSeul,
    AgentMenteur,
    AgentPremierePartieSeulement,
    AgentRejoueur,
    AgentSansUsage,
)
from config import Config
from relais import ErreurRelais, Relais, empreinte

RACINE = Path(__file__).resolve().parent.parent
FICHIER = (RACINE / "exemple" / "calcul_test.py").read_bytes()
ATTENDU = json.loads((RACINE / "exemple" / "attendu.json").read_text())
DEPOT = "dame/relais-test"
COMMIT = "a" * 40


@pytest.fixture
def config(tmp_path):
    return Config(
        secret_mcp="s" * 40,
        modele="modele-test",
        depots_autorises=[DEPOT],
        chemin_base=str(tmp_path / "relais.sqlite3"),
        attente_synchrone=10.0,
        reserve_jetons_par_appel=2_000,
    )


def relais_avec(config, agent, fichier=FICHIER):
    return Relais(config, agent, lambda *a: fichier)


def envoyer_avec_fichier(relais, fil="essai", message="Exécute et commente."):
    return relais.envoyer_message(fil, message, DEPOT, "exemple/calcul_test.py", COMMIT)


def attendre_fin(relais, tache, limite=10.0):
    fin = time.monotonic() + limite
    suivi = relais.suivre_tache(tache, attente_secondes=0.2)
    while suivi["statut"] == "en_cours" and time.monotonic() < fin:
        suivi = relais.suivre_tache(tache, attente_secondes=0.2)
    return suivi


def attendre_solde(relais, tache, limite=10.0):
    """Attend que la tâche soit soldée (thread terminé, consommation ajustée)."""
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if relais.stockage.executer("SELECT ajuste FROM taches WHERE tache=?", (tache,))[0]["ajuste"]:
            return
        time.sleep(0.05)
    raise AssertionError("tâche jamais soldée")


# ===========================================================================
# 1. Preuve d'exécution : fichier vérifié / exécution effectuée / exécution réussie
# ===========================================================================


def test_bloc_conforme_les_trois_constats_positifs(config):
    r = envoyer_avec_fichier(relais_avec(config, AgentExecutant()))
    assert r["statut"] == "terminee"
    p = r["resultat"]["preuve_execution"]
    assert p["statut_global"] == "verifiee"
    assert p["fichier"]["statut"] == "verifie"
    assert p["fichier"]["empreinte_serveur"] == empreinte(FICHIER) == p["fichier"]["empreinte_conteneur"]
    assert p["execution"] == {"statut": "effectuee", "bloc_conforme": True, "conteneur": p["execution"]["conteneur"]}
    assert p["reussite"] == "reussie" and p["code_retour"] == 0
    assert json.loads(p["sortie_execution"]) == ATTENDU
    assert r["resultat"]["traces_execution"], "les traces brutes doivent être renvoyées"


def test_constat_rapporte_par_le_reviseur_lecture_sans_execution(config):
    """Reproduction : lire le fichier et afficher son empreinte, sans l'exécuter."""
    p = envoyer_avec_fichier(relais_avec(config, AgentLecteurSeul()))["resultat"]["preuve_execution"]
    assert p["statut_global"] != "verifiee"
    assert p["statut_global"] == "absente"
    assert p["execution"]["statut"] == "non_conforme"
    assert p["fichier"]["statut"] == "non_verifie"
    assert p["fichier"]["lecture_hors_controle"] == empreinte(FICHIER)
    assert p["code_retour"] is None and p["reussite"] == "inconnue"


def test_bloc_falsifie_imitant_les_lignes_de_controle(config):
    p = envoyer_avec_fichier(relais_avec(config, AgentFaussaire()))["resultat"]["preuve_execution"]
    assert p["statut_global"] == "absente"
    assert p["execution"]["statut"] == "non_conforme"
    assert p["execution"]["bloc_conforme"] is False


def test_trace_tronquee_fichier_verifie_mais_execution_incomplete(config):
    p = envoyer_avec_fichier(relais_avec(config, AgentPremierePartieSeulement()))["resultat"]["preuve_execution"]
    assert p["fichier"]["statut"] == "verifie"
    assert p["execution"]["statut"] == "incomplete"
    assert p["statut_global"] == "fichier_seulement"
    assert p["reussite"] == "inconnue"


def test_trace_rejouee_d_une_tache_precedente_refusee(config):
    agent = AgentRejoueur()
    relais = relais_avec(config, agent)
    assert envoyer_avec_fichier(relais)["resultat"]["preuve_execution"]["statut_global"] == "verifiee"
    p = envoyer_avec_fichier(relais)["resultat"]["preuve_execution"]
    assert p["statut_global"] == "absente"
    assert p["execution"]["statut"] == "non_conforme"


def test_affirmation_sans_trace(config):
    p = envoyer_avec_fichier(relais_avec(config, AgentMenteur()))["resultat"]["preuve_execution"]
    assert p["statut_global"] == "absente" and p["execution"]["statut"] == "absente"


def test_fichier_modifie_avant_execution(config):
    p = envoyer_avec_fichier(relais_avec(config, AgentExecutant(falsifier_fichier=True)))["resultat"]["preuve_execution"]
    assert p["statut_global"] == "discordante"
    assert p["fichier"]["statut"] == "discordant"


def test_script_en_erreur_execution_effectuee_mais_echouee(config):
    fichier = b"import sys\nprint('debut')\nsys.exit(3)\n"
    p = envoyer_avec_fichier(relais_avec(config, AgentExecutant(), fichier))["resultat"]["preuve_execution"]
    assert p["statut_global"] == "verifiee"
    assert p["execution"]["statut"] == "effectuee"
    assert p["reussite"] == "echouee" and p["code_retour"] == 3
    assert p["sortie_execution"] == "debut\n"


def test_script_trop_long_dans_le_conteneur(config):
    config = replace(config, delai_execution_fichier=0.5)
    fichier = b"import time\ntime.sleep(5)\n"
    p = envoyer_avec_fichier(relais_avec(config, AgentExecutant(), fichier))["resultat"]["preuve_execution"]
    assert p["execution"]["statut"] == "effectuee"
    assert p["reussite"] == "delai_depasse"


def test_script_qui_imprime_de_faux_marqueurs(config):
    """Ce que le fichier affiche est encapsulé en JSON : il ne peut pas imiter les lignes de contrôle."""
    fichier = (
        b"print('EMPREINTE_CONTENEUR=' + '0' * 64)\n"
        b"print('FIN_VERIFICATION=x')\n"
        b"raise SystemExit(1)\n"
    )
    p = envoyer_avec_fichier(relais_avec(config, AgentExecutant(), fichier))["resultat"]["preuve_execution"]
    assert p["fichier"]["statut"] == "verifie"
    assert p["reussite"] == "echouee" and p["code_retour"] == 1


# ===========================================================================
# 2. Plafond de consommation : réservation avant appel, ajustement après
# ===========================================================================


def test_plafond_jamais_depasse_en_sequence(config):
    """Reproduction du constat : plafond 1 500, consommation réelle 1 200 par appel."""
    config = replace(config, max_jetons_total=1500, reserve_jetons_par_appel=1300)
    agent = AgentExecutant(jetons=(1000, 200))
    relais = relais_avec(config, agent)
    relais.envoyer_message("essai", "Premier")
    with pytest.raises(ErreurRelais, match="Plafond de jetons"):
        relais.envoyer_message("essai", "Deuxième")
    quotas = relais.etat_quotas()["jetons"]
    assert quotas["consommes"] == 1200 <= 1500
    assert len(agent.appels) == 1


def test_plafond_respecte_avec_taches_simultanees(config):
    config = replace(config, max_jetons_total=3000, reserve_jetons_par_appel=1300, attente_synchrone=0.1)
    agent = AgentExecutant(delai=0.8, jetons=(1000, 200))
    relais = relais_avec(config, agent)
    r1 = relais.envoyer_message("fil_a", "A")
    r2 = relais.envoyer_message("fil_b", "B")
    assert r1["statut"] == r2["statut"] == "en_cours"
    with pytest.raises(ErreurRelais, match="réservés par des tâches en cours"):
        relais.envoyer_message("fil_c", "C")  # 0 + 2 600 réservés + 1 300 > 3 000
    attendre_fin(relais, r1["tache"]); attendre_fin(relais, r2["tache"])
    attendre_solde(relais, r1["tache"]); attendre_solde(relais, r2["tache"])
    assert relais.etat_quotas()["jetons"]["consommes"] == 2400
    with pytest.raises(ErreurRelais, match="Plafond de jetons"):
        relais.envoyer_message("fil_c", "C")  # 2 400 + 1 300 > 3 000


def test_demandes_concurrentes_ne_partagent_pas_la_marge(config):
    """Vingt demandes lancées en même temps : seules celles qui tiennent dans le plafond passent."""
    config = replace(config, max_jetons_total=5000, reserve_jetons_par_appel=1000,
                     attente_synchrone=0.05, max_appels_jour=100, max_executions_jour=1000)
    agent = AgentExecutant(delai=0.5, jetons=(800, 200))
    relais = relais_avec(config, agent)
    acceptees, refusees = [], []

    def demander(i):
        try:
            acceptees.append(relais.envoyer_message(f"fil_{i}", "x")["tache"])
        except ErreurRelais:
            refusees.append(i)

    fils = [threading.Thread(target=demander, args=(i,)) for i in range(20)]
    for f in fils: f.start()
    for f in fils: f.join()
    assert len(acceptees) == 5 and len(refusees) == 15
    for t in acceptees:
        attendre_fin(relais, t); attendre_solde(relais, t)
    assert relais.etat_quotas()["jetons"]["consommes"] == 5000 <= config.max_jetons_total


def test_reserve_sous_estimee_detectee_et_bloquante(config):
    config = replace(config, max_jetons_total=100_000, reserve_jetons_par_appel=1000)
    relais = relais_avec(config, AgentExecutant(jetons=(1000, 200)))
    relais.envoyer_message("essai", "Premier")  # 1 200 réels > 1 000 réservés
    journal = relais.lire_journal("essai")["entrees"]
    assert any(e["contenu"].get("evenement") == "reserve_depassee" for e in journal)
    assert relais.etat_quotas()["jetons"]["reserve_suffisante"] is False
    with pytest.raises(ErreurRelais, match="Réserve insuffisante"):
        relais.envoyer_message("essai", "Deuxième")
    # Relever la réserve au-dessus du maximum observé débloque le relais.
    relais2 = relais_avec(replace(config, reserve_jetons_par_appel=1500), AgentExecutant())
    relais2.envoyer_message("essai", "Troisième")


def test_consommation_non_rapportee_compte_la_reserve(config):
    relais = relais_avec(config, AgentSansUsage())
    relais.envoyer_message("essai", "x")
    assert relais.etat_quotas()["jetons"]["consommes"] == config.reserve_jetons_par_appel


def test_panne_apres_envoi_compte_la_reserve_panne_avant_envoi_non(config):
    relais = relais_avec(config, AgentEnPanne())
    r = relais.envoyer_message("essai", "x")
    assert r["statut"] == "echec" and r["resultat"]["appel_api_lance"] is True
    assert relais.etat_quotas()["jetons"]["consommes"] == config.reserve_jetons_par_appel

    def telechargement_en_panne(*a):
        raise RuntimeError("GitHub indisponible")

    relais2 = Relais(replace(config, chemin_base=config.chemin_base + "2"), AgentExecutant(), telechargement_en_panne)
    r2 = envoyer_avec_fichier(relais2)
    assert r2["statut"] == "echec" and r2["resultat"]["appel_api_lance"] is False
    assert relais2.etat_quotas()["jetons"]["consommes"] == 0


def test_limite_executions_reservee_avant_appel(config):
    config = replace(config, max_executions_jour=4, max_appels_outil_par_reponse=3)
    relais = relais_avec(config, AgentExecutant())
    envoyer_avec_fichier(relais)  # 0 effectuée + 3 réservées ≤ 4 ; 1 exécution réelle
    envoyer_avec_fichier(relais)  # 1 + 3 ≤ 4 ; 2 exécutions réelles au total
    with pytest.raises(ErreurRelais, match="Limite d'exécutions"):
        envoyer_avec_fichier(relais)  # 2 + 3 > 4 : refusé avant tout appel
    assert relais.etat_quotas()["executions"]["effectuees"] == 2


def test_limite_appels_bloquante_sans_appel_agent(config):
    config = replace(config, max_appels_jour=1)
    agent = AgentExecutant()
    relais = relais_avec(config, agent)
    relais.envoyer_message("essai", "Premier")
    with pytest.raises(ErreurRelais, match="Limite d'appels"):
        relais.envoyer_message("essai", "Second")
    assert len(agent.appels) == 1


def test_pause(config):
    agent = AgentExecutant()
    with pytest.raises(ErreurRelais, match="pause"):
        relais_avec(replace(config, pause=True), agent).envoyer_message("essai", "x")
    assert agent.appels == []


def test_bornes_par_appel_transmises(config):
    agent = AgentExecutant()
    relais_avec(config, agent).envoyer_message("essai", "x")
    assert agent.appels[0]["max_jetons_sortie"] == config.max_jetons_sortie_par_appel
    assert agent.appels[0]["max_appels_outil"] == config.max_appels_outil_par_reponse
    assert 0 < agent.appels[0]["delai_secondes"] <= config.delai_max_tache


def test_refus_ne_consomme_pas_de_quota(config):
    relais = relais_avec(config, AgentExecutant())
    with pytest.raises(ErreurRelais):
        relais.envoyer_message("essai", "x", DEPOT, "a.py", "main")
    q = relais.etat_quotas()
    assert q["appels"]["utilises"] == 0 and q["jetons"]["reserves_en_cours"] == 0


# ===========================================================================
# 3. Échéance stricte
# ===========================================================================


def test_reponse_tardive_jamais_terminee(config):
    """Reproduction du constat : une tâche plus longue que sa limite ne finit plus « terminee »."""
    config = replace(config, attente_synchrone=0.1, delai_max_tache=0.5)
    agent = AgentExecutant(delai=1.2)  # n'honore pas l'échéance
    relais = relais_avec(config, agent)
    r = relais.envoyer_message("essai", "x")
    suivi = attendre_fin(relais, r["tache"])
    assert suivi["statut"] == "expiree"
    attendre_solde(relais, r["tache"])
    final = relais.suivre_tache(r["tache"])
    assert final["statut"] == "expiree", "un résultat tardif ne doit jamais changer le statut"
    assert "resultat" not in final
    assert final["resultat_tardif_hors_delai"]["reponse_agent"]
    # Non chaîné : l'historique du fil ignore la réponse tardive.
    assert relais.stockage.executer("SELECT reponse_precedente FROM fils WHERE fil='essai'")[0][0] is None
    evenements = [e["contenu"].get("evenement") for e in relais.lire_journal("essai")["entrees"]]
    assert "echeance_depassee" in evenements and "reponse_tardive_ignoree" in evenements
    assert "agent" not in [e["auteur"] for e in relais.lire_journal("essai")["entrees"]]
    # La consommation réelle d'une réponse tardive est quand même comptée.
    assert relais.etat_quotas()["jetons"]["consommes"] == 1200


def test_echeance_avec_annulation_compte_la_reserve(config):
    config = replace(config, attente_synchrone=0.1, delai_max_tache=0.4)
    relais = relais_avec(config, AgentExecutant(delai=5.0, respecte_echeance=True))
    r = relais.envoyer_message("essai", "x")
    assert attendre_fin(relais, r["tache"])["statut"] == "expiree"
    attendre_solde(relais, r["tache"])
    entree = [e for e in relais.lire_journal("essai")["entrees"]
              if e["contenu"].get("evenement") == "reponse_abandonnee_a_echeance"][0]
    assert entree["contenu"]["annulation_demandee"] is True
    assert relais.etat_quotas()["jetons"]["consommes"] == config.reserve_jetons_par_appel


def test_fil_occupe_tant_que_l_appel_expire_n_est_pas_fini(config):
    """Reproduction du constat : après expiration, l'ancien appel continue ; aucun nouvel
    appel ne doit partir sur le même fil, donc sur le même conteneur, avant sa fin effective."""
    config = replace(config, attente_synchrone=5.0, delai_max_tache=0.5)
    agent = AgentExecutant()
    relais = relais_avec(config, agent)
    envoyer_avec_fichier(relais, message="Premier, rapide")       # resp_1, conteneur C1
    conteneur_initial = agent.appels[0]["conteneur"]

    agent.delai = 1.5                                               # n'honore pas l'échéance
    r = envoyer_avec_fichier(relais, message="Deuxième, trop long")
    assert r["statut"] == "expiree" and r["appel_termine"] is False
    with pytest.raises(ErreurRelais, match="reste occupé"):
        relais.envoyer_message("essai", "Tentative pendant l'appel expiré")
    assert len(agent.appels) == 2, "la tentative ne doit atteindre l'agent"

    attendre_solde(relais, r["tache"])
    assert relais.suivre_tache(r["tache"])["appel_termine"] is True
    agent.delai = 0.0
    envoyer_avec_fichier(relais, message="Troisième, après la fin effective")

    assert agent.simultanes_max_par_conteneur == 1, "jamais deux appels sur un même conteneur"
    assert agent.appels[2]["reponse_precedente"] == "resp_1", "l'historique repart de la dernière réponse à temps"
    assert agent.appels[2]["conteneur"] not in (None, conteneur_initial), \
        "reprise sur un nouveau conteneur (arrêt de l'ancien appel non confirmé)"
    evenements = [e["contenu"].get("evenement") for e in relais.lire_journal("essai")["entrees"]]
    assert "conteneur_abandonne" in evenements


def test_arret_non_confirme_a_echeance_nouveau_conteneur(config):
    config = replace(config, attente_synchrone=5.0, delai_max_tache=0.4)
    agent = AgentExecutant(respecte_echeance=True, arret_confirme=False)
    relais = relais_avec(config, agent)
    envoyer_avec_fichier(relais)
    assert agent.appels[0]["conteneur"] is not None
    agent.delai = 5.0
    r = envoyer_avec_fichier(relais)
    attendre_solde(relais, r["tache"])
    agent.delai = 0.0
    envoyer_avec_fichier(relais)
    assert agent.appels[2]["conteneur"] not in (None, agent.appels[0]["conteneur"])


def test_arret_confirme_a_echeance_conteneur_conserve(config):
    config = replace(config, attente_synchrone=5.0, delai_max_tache=0.4)
    agent = AgentExecutant(respecte_echeance=True, arret_confirme=True)
    relais = relais_avec(config, agent)
    envoyer_avec_fichier(relais)
    conteneur = agent.appels[0]["conteneur"]
    agent.delai = 5.0
    r = envoyer_avec_fichier(relais)
    attendre_solde(relais, r["tache"])
    agent.delai = 0.0
    envoyer_avec_fichier(relais)
    assert agent.appels[2]["conteneur"] == conteneur


def test_erreur_apres_envoi_nouveau_conteneur(config):
    agent = AgentExecutant()
    relais = relais_avec(config, agent)
    envoyer_avec_fichier(relais)
    assert relais.stockage.executer("SELECT conteneur FROM fils WHERE fil='essai'")[0][0] is not None
    relais.adaptateur = AgentEnPanne()
    relais.envoyer_message("essai", "x")
    assert relais.stockage.executer("SELECT conteneur FROM fils WHERE fil='essai'")[0][0] is None


def test_tache_en_cours_au_redemarrage_interrompue_et_reserve_comptee(config):
    relais = relais_avec(config, AgentExecutant())
    relais.stockage.executer("INSERT INTO fils (fil, conteneur, cree_le) VALUES ('essai', 'cntr_ancien', 'x')")
    relais.stockage.executer(
        "INSERT INTO taches (tache, fil, statut, debut, echeance, jour, jetons_reserves, executions_reservees) "
        "VALUES ('t1', 'essai', 'en_cours', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', "
        "'2026-01-01', 777, 3)"
    )
    relais2 = relais_avec(config, AgentExecutant())
    assert relais2.suivre_tache("t1")["statut"] == "interrompue"
    assert relais2.etat_quotas()["jetons"]["consommes"] == 777
    # L'appel distant a pu continuer pendant le redémarrage : le conteneur n'est plus réutilisé.
    assert relais2.stockage.executer("SELECT conteneur FROM fils WHERE fil='essai'")[0][0] is None


# ===========================================================================
# 4. Historique, journal, validation
# ===========================================================================


def test_historique_chaine_et_journal_complet(config):
    agent = AgentExecutant()
    relais = relais_avec(config, agent)
    envoyer_avec_fichier(relais)
    r2 = relais.envoyer_message("essai", "Question de suivi sans fichier.")
    assert agent.appels[0]["reponse_precedente"] is None
    assert agent.appels[1]["reponse_precedente"] == "resp_1"
    assert agent.appels[1]["conteneur"] == agent.appels[0]["conteneur"]
    assert r2["resultat"]["preuve_execution"]["statut_global"] == "sans_fichier"
    journal = relais.lire_journal("essai")["entrees"]
    assert [e["auteur"] for e in journal] == ["claude", "agent", "claude", "agent"]
    assert journal[0]["contenu"]["fichier"]["commit"] == COMMIT
    empreintes = [e["empreinte_chainee"] for e in journal]
    assert len(set(empreintes)) == len(empreintes)


def test_un_seul_message_en_cours_par_fil(config):
    config = replace(config, attente_synchrone=0.1)
    relais = relais_avec(config, AgentExecutant(delai=0.8))
    r = relais.envoyer_message("essai", "x")
    with pytest.raises(ErreurRelais, match="déjà une tâche en cours"):
        relais.envoyer_message("essai", "y")
    assert attendre_fin(relais, r["tache"])["statut"] == "terminee"


@pytest.mark.parametrize(
    "depot,chemin,commit,motif",
    [
        ("autre/depot", "a.py", "a" * 40, "non autorisé"),
        (DEPOT, "a.py", "main", "SHA complet"),
        (DEPOT, "../secret.py", "a" * 40, "Chemin"),
        (DEPOT, "/etc/passwd", "a" * 40, "Chemin"),
        (DEPOT, None, "a" * 40, "ensemble"),
    ],
)
def test_references_invalides(config, depot, chemin, commit, motif):
    with pytest.raises(ErreurRelais, match=motif):
        relais_avec(config, AgentExecutant()).envoyer_message("essai", "x", depot, chemin, commit)


def test_fil_et_message_invalides(config):
    relais = relais_avec(config, AgentExecutant())
    with pytest.raises(ErreurRelais, match="fil"):
        relais.envoyer_message("fil avec espaces", "x")
    with pytest.raises(ErreurRelais, match="vide"):
        relais.envoyer_message("essai", "   ")
