# Relais Claude – agent OpenAI (prototype)

Ce prototype permet à Claude, depuis Claude web, d'échanger directement avec un agent OpenAI
via un connecteur MCP personnalisé, sans copier-coller :

1. Claude envoie une consigne à l'agent dans un « fil » ;
2. les deux travaillent sur le même fichier, désigné par un commit GitHub précis ;
3. l'agent l'exécute dans un conteneur OpenAI isolé ;
4. Claude lui répond dans le même fil, dont l'historique est conservé ;
5. chaque échange est inscrit dans un journal horodaté que Claude peut relire.

**Ce que le prototype ne fait pas encore.** Il ne travaille que pendant une conversation Claude :
rien ne se poursuit seul quand la conversation s'arrête. C'est une étape ultérieure.

---

## 1. Contenu du dossier

| Fichier | Rôle |
|---|---|
| `serveur.py` | Serveur MCP (point d'accès `/mcp`), authentification, outils exposés à Claude |
| `relais.py` | Logique : fichier GitHub, fils, tâches, preuve d'exécution, limites, journal |
| `adaptateur_openai.py` | Seul fichier qui appelle l'API OpenAI (Responses + Code Interpreter) |
| `config.py` | Lecture des variables d'environnement |
| `exemple/calcul_test.py` | Fichier de test déterministe |
| `exemple/attendu.json` | Sortie attendue de ce fichier |
| `tests/` | Tests automatiques (agent simulé, aucun appel réseau) |
| `Dockerfile`, `requirements.txt`, `.env.exemple` | Déploiement |

Outils vus par Claude :

| Outil | Usage |
|---|---|
| `envoyer_message(fil, message, depot?, chemin?, commit?)` | Message à l'agent, avec fichier facultatif |
| `suivre_tache(tache, attente_secondes?)` | Suivi d'une exécution longue |
| `lire_journal(fil, depuis?)` | Historique complet et horodaté du fil |
| `etat_quotas()` | Consommation et limites |

---

## 2. Ce que vous devez faire vous-même

### 2.1 OpenAI : clé et plafond de dépense

1. Sur platform.openai.com, créez un **projet dédié** au relais et une **clé API rattachée à ce projet**.
2. Distinguez bien deux mécanismes :
   - une **alerte budgétaire** envoie une notification mais **n'arrête pas** les appels ;
   - seul un mécanisme bloquant empêche la dépense. Le plus sûr : un compte en **crédits prépayés,
     recharge automatique désactivée**, avec un petit montant (par exemple 10 €). Si la console propose
     une limite de dépense bloquante au niveau du projet, activez-la aussi, en vérifiant qu'elle est
     bien décrite comme bloquante et pas seulement comme une notification.
3. Choisissez un modèle compatible avec l'outil Code Interpreter (voir la page de tarification et la
   documentation de l'outil). Son nom ira dans `OPENAI_MODELE`.
4. Ne collez jamais la clé dans une conversation : elle ne doit exister que dans les variables
   d'environnement de l'hébergeur.

### 2.2 GitHub : dépôt de test

1. Créez un dépôt **public** de test (aucune donnée sensible) ; le relais lira le fichier sans jeton.
2. Déposez-y `exemple/calcul_test.py`, et ce dossier entier si vous déployez depuis GitHub.
3. Le commit doit être désigné par son **SHA complet (40 caractères)**, jamais par un nom de branche :
   c'est ce qui garantit que Claude et l'agent travaillent sur la même version.

### 2.3 Hébergement

N'importe quel service qui exécute un conteneur Docker et fournit une adresse HTTPS publique convient.
Exigences :

- **une seule instance** (l'état des tâches en cours est en mémoire, la base SQLite est locale) ;
- un **disque persistant** monté sur `/donnees`, sinon journal et compteurs sont effacés à chaque
  redéploiement, ce qui remet aussi les compteurs de dépense à zéro ;
- une offre **sans mise en veille** si possible : le réveil peut dépasser le délai d'un appel d'outil ;
- adresse joignable depuis l'internet public (Claude se connecte depuis les serveurs d'Anthropic).

Variables d'environnement : voir `.env.exemple`. Obligatoires : `OPENAI_API_KEY`, `OPENAI_MODELE`,
`MCP_SECRET`, `DEPOTS_AUTORISES`.

Vérification après déploiement, avant de toucher à Claude :

```bash
curl https://<votre-hote>/sante                       # attendu : {"statut": "ok"}
curl -i -X POST https://<votre-hote>/mcp              # attendu : HTTP 401 (pas de secret)
```

### 2.4 Connecteur dans Claude web

1. Dans les réglages des connecteurs : « Ajouter un connecteur personnalisé ».
2. Adresse : `https://<votre-hote>/mcp`.
3. Authentification : en-tête fixe `Authorization` avec la valeur `Bearer <MCP_SECRET>`.
4. **Repli** si l'interface ne permet pas d'en-tête fixe : adresse `https://<votre-hote>/s/<MCP_SECRET>/mcp`
   sans en-tête. C'est moins sûr (le secret peut apparaître dans les journaux d'accès de l'hébergeur) :
   à réserver au test, puis changer le secret.
5. Ouvrez une **nouvelle conversation**, activez le connecteur, et approuvez les appels d'outils.

---

## 3. Scénario du test de connexion

Dans la nouvelle conversation, demandez à Claude de dérouler ce scénario :

1. `etat_quotas()` : vérifie l'accès et les limites.
2. `envoyer_message(fil="test-connexion", message="Exécute le fichier joint et rends compte du résultat.",
   depot="<proprietaire>/<depot>", chemin="exemple/calcul_test.py", commit="<SHA complet>")`.
3. Si le statut est `en_cours` : `suivre_tache(tache=..., attente_secondes=20)` jusqu'à la fin.
4. Claude calcule de son côté l'empreinte SHA-256 du même fichier au même commit et compare la sortie
   à `exemple/attendu.json`.
5. `envoyer_message(fil="test-connexion", message="<question technique de suivi>")`, sans fichier :
   par exemple sur l'ordre de convergence de la méthode de Simpson et l'erreur observée.
6. `lire_journal(fil="test-connexion")`.
7. Claude vous remet le bilan.

### Critères de réussite

| N° | Critère | Où le vérifier |
|---|---|---|
| 1 | L'agent a reçu la consigne et répondu | `reponse_agent` non vide, statut `terminee` |
| 2 | Même fichier des deux côtés | `preuve_execution.fichier.statut = verifie`, empreinte identique à celle calculée par Claude |
| 3 | Exécution réelle et correcte | `statut_global = verifiee`, `reussite = reussie`, `sortie_execution` identique à `attendu.json` (Simpson : tolérance 1e-14) |
| 4 | Historique conservé | La réponse au 2e message s'appuie explicitement sur le 1er échange |
| 5 | Journal complet sans copier-coller | `lire_journal` : entrées claude / agent / claude / agent, horodatées |

---

## 4. Preuve d'exécution : trois constats distincts

Le relais ne se fie **jamais** au texte du modèle. Il calcule lui-même l'empreinte SHA-256 du
fichier au commit indiqué, dépose ce fichier dans le conteneur et fournit à l'agent un **bloc de
contrôle propre à la tâche** (avec un jeton aléatoire). Seule la trace de l'outil d'exécution dont
le code est **exactement** ce bloc fait foi. Ce bloc affiche l'empreinte du fichier présent, lance
le fichier, puis écrit son résultat (code retour, sortie, erreurs) encodé en JSON sur une seule
ligne : ce que le fichier affiche lui-même ne peut donc pas imiter les lignes de contrôle.

| Constat | Valeurs | Signification |
|---|---|---|
| `fichier.statut` | `verifie` / `discordant` / `non_verifie` | Empreinte observée par le bloc de contrôle, comparée à celle du relais |
| `execution.statut` | `effectuee` / `incomplete` / `non_conforme` / `absente` | Le bloc conforme a-t-il été exécuté jusqu'à sa dernière ligne ? |
| `reussite` | `reussie` / `echouee` / `delai_depasse` / `inconnue` | Code retour du fichier (0, autre, délai d'exécution dépassé) |

`statut_global` résume les deux premiers constats ; la réussite est donnée à part :

- `verifiee` : fichier vérifié **et** bloc exécuté jusqu'au bout ;
- `fichier_seulement` : fichier vérifié, exécution non établie (trace interrompue) ;
- `discordante` : le fichier exécuté n'est pas celui du commit ;
- `absente` : rien n'est établi. C'est le cas d'un agent qui lit le fichier et affiche son
  empreinte sans l'exécuter, qui modifie le bloc, ou qui rejoue la trace d'une tâche
  précédente. Une lecture hors bloc est signalée dans `fichier.lecture_hors_controle`,
  pour information seulement.

Limite : les traces proviennent de l'API OpenAI. La garantie repose sur l'intégrité de l'API,
pas sur une signature cryptographique de l'exécution.

---

## 5. Limites de dépense

### Ce que le relais garantit

Avant chaque appel, le relais **réserve** une quantité de jetons (`RESERVE_JETONS_PAR_APPEL`)
et un nombre d'exécutions (`MAX_APPELS_OUTIL_PAR_REPONSE`), dans la même transaction que le
contrôle. Un appel n'est accepté que si :

`jetons consommés + jetons réservés par les tâches en cours + réserve de cet appel ≤ MAX_JETONS_TOTAL`

(et de même pour les exécutions du jour). Deux demandes simultanées ne peuvent donc pas se
partager la même marge. Après l'appel, la réservation est remplacée par la consommation réelle.

Quand la consommation réelle est inconnue (échéance dépassée, panne après l'envoi, redémarrage
du serveur, consommation non rapportée), **la réservation entière est comptée comme consommée**.

### Ce qu'il ne peut pas garantir seul

L'API ne permet pas de borner à l'avance les jetons **d'entrée** d'une réponse qui enchaîne
plusieurs appels d'outil sur un historique croissant. Le plafond est donc garanti **tant que
chaque appel reste sous sa réserve**. Si un appel la dépasse, le relais l'inscrit au journal
(`reserve_depassee`), l'affiche dans `etat_quotas`, et **refuse tout nouvel appel** jusqu'à ce que
`RESERVE_JETONS_PAR_APPEL` soit relevée au-dessus du maximum observé. Le dépassement possible est
alors limité à l'écart de ce seul appel ; le plafond monétaire strict reste celui du côté OpenAI
(§ 2.1).

### Réglages

| Variable | Défaut | Effet |
|---|---|---|
| `MAX_JETONS_TOTAL` | 300 000 | Plafond cumulé depuis l'installation |
| `RESERVE_JETONS_PAR_APPEL` | 50 000 | Réserve prudente par appel (à relever si le journal signale un dépassement) |
| `MAX_APPELS_JOUR` | 20 | Messages envoyés à l'agent par jour (UTC) |
| `MAX_EXECUTIONS_JOUR` | 20 | Appels à l'outil d'exécution par jour (UTC), réservés avant l'appel |
| `MAX_JETONS_SORTIE_PAR_APPEL` | 4 000 | Plafond transmis à l'API pour chaque réponse |
| `MAX_APPELS_OUTIL_PAR_REPONSE` | 3 | Exécutions maximales dans une réponse ; c'est aussi la réserve d'exécutions par appel |
| `PAUSE` | 0 | `1` : tout appel est refusé |

Avec les valeurs par défaut, au plus 6 appels tiennent dans le plafond (6 × 50 000 = 300 000).

Autres protections : un seul message en cours par fil, dépôts limités à `DEPOTS_AUTORISES`,
fichier limité à 200 ko, message limité à 20 000 caractères.

---

## 6. Échéance des tâches

- Chaque tâche a une **échéance stricte** (`DELAI_MAX_TACHE`, 600 s par défaut), inscrite dès
  sa création. À l'échéance, un minuteur la passe en `expiree` ; `suivre_tache` le fait aussi si
  le minuteur n'est pas encore passé.
- Le statut `terminee` n'est attribué que si la réponse arrive **avant** l'échéance. Une réponse
  tardive n'est jamais présentée comme terminée : elle est conservée à part
  (`resultat_tardif_hors_delai`, pour examen seulement), **n'est pas chaînée** à l'historique du
  fil, et sa consommation réelle est comptée.
- Après expiration, **le fil reste occupé jusqu'à la fin effective de l'ancien appel**
  (`suivre_tache` indique `appel_termine`). Deux appels ne peuvent donc jamais utiliser le même
  conteneur en même temps.
- Si l'arrêt de l'ancien appel n'est pas confirmé par OpenAI (réponse tardive, annulation non
  confirmée, erreur après l'envoi, redémarrage du serveur), le conteneur du fil est abandonné :
  le message suivant repart dans un **nouveau conteneur isolé** (événement `conteneur_abandonne`
  au journal). L'historique repart de la dernière réponse obtenue dans les délais.
- Côté OpenAI, en mode arrière-plan (par défaut), le relais **demande l'annulation** de la réponse
  à l'échéance. La documentation ne dit pas si l'annulation arrête la facturation déjà engagée :
  la réservation entière est donc comptée. Si Code Interpreter ne fonctionne pas en arrière-plan,
  `OPENAI_MODE_ARRIERE_PLAN=0` repasse en appel synchrone, coupé côté client à l'échéance sans
  annulation possible côté OpenAI.
- Le fichier lui-même est limité à `DELAI_EXECUTION_FICHIER` (120 s) dans le conteneur ;
  au-delà, `reussite = delai_depasse`.

---

## 7. Tests locaux

```bash
pip install -r requirements.txt
python -m pytest -q
```

44 tests, sans réseau, avec des agents simulés qui exécutent réellement le code reçu.
Ils couvrent :

- **la preuve** : bloc conforme ; lecture sans exécution ; bloc falsifié imitant les lignes de
  contrôle ; trace tronquée ; trace rejouée d'une autre tâche ; affirmation sans trace ; fichier
  modifié ; script en erreur ; script trop long ; script imprimant de faux marqueurs ;
- **le plafond** : appels successifs, tâches simultanées, vingt demandes concurrentes,
  réserve sous-estimée, consommation inconnue, panne avant et après envoi, exécutions réservées ;
- **l'échéance** : réponse tardive jamais `terminee` ni chaînée, annulation, fil occupé jusqu'à
  la fin effective de l'appel expiré (jamais deux appels sur un même conteneur), nouveau conteneur
  quand l'arrêt n'est pas confirmé, redémarrage ;
- l'authentification et le dialogue MCP complet par HTTP réel, le journal, la validation des entrées.

Les corrections ont été contrôlées par mutation : en réintroduisant chacun des défauts signalés
lors des revues (contrôle du budget a posteriori, bloc non contrôlé, échéance non appliquée, fil
libéré dès l'expiration, conteneur jamais abandonné), au moins un test échoue à chaque fois.

---

## 8. Points non vérifiés à ce stade

- **`adaptateur_openai.py` n'a pas été exécuté contre l'API OpenAI réelle** : l'environnement de
  développement n'y avait pas accès. Les noms de paramètres suivent la documentation consultée le
  2 novembre 2026 (`containers.create`, `containers.files.create`, `include=["code_interpreter_call.outputs"]`,
  `max_tool_calls`, `previous_response_id`, `background`, `responses.cancel`). La documentation ne confirme
  pas que Code Interpreter fonctionne en mode arrière-plan, ni que `responses.retrieve` accepte `include` ;
  l'adaptateur prévoit un repli dans les deux cas. Le premier test réel peut demander une correction à cet endroit ;
  l'erreur exacte sera visible dans le journal (`auteur = relais`).
- Le chemin du fichier dans le conteneur est lu dans la réponse de l'API ; à défaut, le relais
  suppose `/mnt/data/<nom>`.
- La possibilité de saisir un en-tête fixe dans le connecteur dépend de votre offre Claude ;
  l'adresse de repli couvre le cas contraire.

## 9. Confidentialité

Ce prototype ne doit recevoir **aucune donnée professionnelle réelle** : le fichier transite par
GitHub, l'hébergeur, Anthropic et OpenAI. L'usage de données d'entreprise demande l'accord
préalable de l'organisation concernée.
