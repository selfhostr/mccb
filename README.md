# PST → IMAP / CalDAV / CardDAV

Outil web (Docker) pour migrer en masse des boîtes Outlook/POP (fichiers PST) vers un serveur IMAP
(mails), CalDAV (rendez-vous) et CardDAV (contacts). Les migrations se lancent boîte par boîte, avec
des vérifications à chaque étape. On peut mettre en pause, arrêter et reprendre sans créer de
doublons. Spécification complète : `docs/SPEC.md`.

## Sommaire

1. [Démarrage](#démarrage)
2. [Changer le mot de passe admin](#changer-le-mot-de-passe-admin)
3. [Variables d'environnement](#variables-denvironnement)
4. [Format CSV d'import des comptes](#format-csv-dimport-des-comptes)
5. [Workflow](#workflow)
6. [Fonctionnement des reprises](#fonctionnement-des-reprises)
7. [Fonctionnement général](#fonctionnement-général)
8. [Derrière un reverse proxy](#derrière-un-reverse-proxy)
9. [Limites connues](#limites-connues)
10. [Tests](#tests)

## Démarrage

```bash
cp .env.example .env
# éditer .env : ADMIN_PASSWORD (obligatoire) et SECRET_KEY (recommandé)
docker compose up -d --build
curl http://localhost:22362/api/health
```

Générer une `SECRET_KEY` :

```bash
openssl rand -base64 32
# ou : python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

L'interface est servie sur **http://localhost:22362/** (ou `http://<hôte>:22362/`). `GET /api/health`
est public et indique aussi si le worker est vivant (heartbeat de moins de 60 s).

Le compose lance deux services avec la même image :

| service  | rôle |
|----------|------|
| `web`    | API FastAPI + interface statique (`app/web/`), port 22362 |
| `worker` | file de jobs (une boîte = un job) et validation des PST téléversés |

Toutes les données sont dans `./data` (monté sur `/data`) :

- `app.db` : base SQLite (WAL) ;
- `pst/` : fichiers PST ;
- `reports/job-N.json` : rapports ;
- `logs/job-N.log` : journaux ;
- `secret.key` : clé générée si `SECRET_KEY` est vide. **Sauvegardez-la** : sans elle (ou sans la même
  `SECRET_KEY`), les mots de passe de boîtes enregistrés ne peuvent plus être déchiffrés.

Si `SECRET_KEY` est définie, elle prend le pas sur `data/secret.key`. La changer rend les mots de
passe de boîtes déjà enregistrés indéchiffrables (il faut les ressaisir) et invalide les sessions.

## Changer le mot de passe admin

1. Modifier `ADMIN_PASSWORD` dans `.env`.
2. Recréer les conteneurs pour qu'ils relisent `.env` : `docker compose up -d` (un simple
   `restart` ne relit pas `env_file`).
3. Toutes les sessions existantes sont **invalidées** (le cookie de session embarque une empreinte du
   mot de passe admin) : il faut se reconnecter avec le nouveau mot de passe.

Après 5 échecs de connexion en 5 minutes depuis la même IP, `POST /api/login` répond 429. Chaque échec
est ralenti de 0,5 s. Le compteur est en mémoire du process `web` : un redémarrage le remet à zéro.

## Variables d'environnement

Elles se définissent dans `.env` (lu par les services `web` et `worker`).

| variable | défaut | rôle |
|----------|--------|------|
| `ADMIN_PASSWORD` | *(vide)* | Mot de passe de l'interface. **Obligatoire** : vide, aucune connexion n'est possible. |
| `SECRET_KEY` | *(vide)* | Clé de chiffrement des mots de passe de boîtes et de signature des sessions. Vide : générée dans `data/secret.key`. |
| `UPLOAD_CHUNK_SIZE` | `33554432` (32 Mio) | Taille des chunks d'upload, en octets (16 à 64 Mio recommandé). **Ne pas la changer en cours de migration**, voir [reprises](#fonctionnement-des-reprises). |
| `UPLOAD_DISK_MARGIN` | `536870912` (512 Mio) | Marge d'espace disque exigée en plus de la taille du PST. |
| `WORKER_CONCURRENCY` | `2` | Nombre de boîtes migrées simultanément par le worker. |
| `VERIFY_SAMPLE` | `20` | Nombre de mails relus pour le contrôle par échantillon en fin de job. |
| `IMAP_MAX_RETRIES` | `5` | Tentatives sur erreur IMAP temporaire (backoff exponentiel). |
| `MAX_CHILD_CRASHES` | `3` | Crashs du sous-processus (libpff) tolérés par job avant de le passer en `failed` (minimum 1). |
| `CHILD_TERM_GRACE` | `20` | Secondes laissées aux sous-processus pour s'arrêter sur SIGTERM avant SIGKILL. |
| `COOKIE_SECURE` | `0` | `1` = cookie de session `Secure` (HTTPS derrière un reverse proxy). Le cookie est aussi `Secure` si la requête arrive en HTTPS. |
| `SESSION_MAX_AGE` | `604800` (7 jours) | Durée de vie d'une session, en secondes. |
| `WORKER_STALE_SECONDS` | `60` | Un job `running` dont le heartbeat est plus vieux est considéré orphelin et remis en file. |
| `PORT` | `22362` | Port d'écoute du service `web` dans le conteneur (le compose publie `22362`). |
| `DATA_DIR` | `/data` | Répertoire de données (fixé par le compose). |
| `PSTMIG_ENGINE` | `app.engine` | Module engine ; sert uniquement aux tests. |

`.env.example` ne liste que les variables courantes ; `SESSION_MAX_AGE`, `WORKER_STALE_SECONDS`,
`CHILD_TERM_GRACE` et `PORT` sont lues par `app/server/config.py` et optionnelles.

## Format CSV d'import des comptes

Import via l'interface (page Comptes) ou `POST /api/accounts/import-csv` (multipart : `file` +
`server_id`). Un modèle est téléchargeable via `GET /api/accounts/csv-template`.

| colonne | obligatoire | contenu |
|---------|-------------|---------|
| `email` | oui | adresse de la boîte (doit ressembler à `x@domaine.tld`) |
| `password` | oui | mot de passe de la boîte (non vide) |
| `login` | non | identifiant de connexion IMAP/DAV s'il diffère de l'adresse ; vide = l'adresse |

- **Séparateur** : `;` ou `,`, détecté sur la première ligne non vide.
- **Entête** : optionnel. Reconnu si la première ligne contient des noms comme `email`, `e-mail`,
  `mail`, `adresse`, `courriel`, `password`, `mot de passe`, `mdp`, `pass`, `login`, `identifiant` ;
  les colonnes sont alors repérées par leur nom. Sans entête, l'ordre est `email;password;login`.
- **Encodage** : UTF-8 (avec ou sans BOM), UTF-16 (avec BOM) ou, à défaut, cp1252.
- Les lignes vides sont ignorées. Les lignes invalides (adresse invalide, mot de passe manquant) sont
  rejetées une par une et rapportées avec leur numéro de ligne ; les autres sont importées.
- Réponse : `{created, updated, errors:[{line,msg}]}` (une adresse déjà présente sur ce serveur est
  mise à jour).

Exemple :

```csv
email;password;login
jean.dupont@exemple.fr;MotDePasse1;
marie.martin@exemple.fr;S3cret;mmartin
```

Un mot de passe contenant le séparateur doit être entouré de guillemets (`"a;b"`).

## Workflow

1. **Serveurs.** Créer un serveur : hôte et port IMAP, sécurité (`ssl`, `starttls`, `none`), vérification
   TLS (désactivable), dossier racine optionnel (`root_folder`, ex. `Import PST`), URL CalDAV/CardDAV
   (vides = auto-découverte, voir [DAV](#dav)), nombre de connexions parallèles (`workers`). Un bouton
   de test vérifie la connexion.
2. **Comptes.** Ajouter les boîtes une à une ou par [CSV](#format-csv-dimport-des-comptes). Le login IMAP est
   testé à l'ajout, puis de nouveau avant le run (`precheck`). Pour chaque compte on peut activer ou
   non mail, agenda et contacts.
3. **Upload des PST.** Glisser-déposer le fichier dans la page Uploads. Il est découpé en chunks
   (32 Mio par défaut), envoyés 3 en parallèle avec retry automatique. Chaque chunk est vérifié par
   SHA-256 côté serveur. L'upload est **reprenable** : après un rafraîchissement ou une coupure, renvoyer
   le même fichier (clé `nom + taille + date de modification`) reprend aux chunks manquants. L'espace
   disque est contrôlé avant l'upload (507 s'il manque de la place).
4. **Validation / scan.** À la fin de l'upload, le serveur contrôle la taille, puis le worker vérifie la
   signature `!BDN` et analyse tout le PST (comptage des mails, rendez-vous et contacts par dossier)
   dans un sous-processus. Le PST passe en `valid` ou `invalid` (avec le motif).
5. **Aperçu (.zip).** Sur un PST `valid`, télécharger `GET /api/psts/{id}/preview?limit=20` (limite de 1 à
   200) : un zip d'un échantillon réparti de mails (`.eml`), rendez-vous (`.ics`) et contacts (`.vcf`),
   rangés par dossier. À ouvrir **avant de lancer** pour vérifier les accents (sujets, corps, noms de
   dossiers), le nom et le type des pièces jointes et les images intégrées. Génération en
   sous-processus, délai maximal 120 s ; erreur 409 si le PST n'est pas valide, 500 si libpff plante.
6. **Association.** Rattacher un PST valide à chaque compte.
7. **Runs.** Sélectionner des comptes puis « Lancer » (`POST /api/runs`) : un job par boîte, mis en file.
   Le worker en exécute `WORKER_CONCURRENCY` à la fois. Phases : `precheck` (IMAP, DAV, quota), `mail`,
   `calendar`, `contacts`, `verify`.
8. **Pause / reprise / arrêt.** Depuis le détail du job : pause, reprise, arrêt, relance complète, ou
   « relancer les erreurs ». Les commandes sont prises en compte entre deux éléments.
9. **Suivi et rapport.** Progression par phase, dossier courant, débit, ETA, journal en direct (SSE),
   liste des erreurs. En fin de job, rapport JSON (`GET /api/jobs/{id}/report`, aussi dans
   `data/reports/`) : envoyés, présents, erreurs, avertissements, ignorés. Un job est `done` si tout
   est passé, `done_with_errors` sinon (erreurs, vérification en échec ou éléments non terminés).

## Fonctionnement des reprises

**États d'un élément.** Chaque mail, contact ou rendez-vous a une ligne dans la table `items` :
`pending` → `inflight` (marqué juste avant l'APPEND) → `ok` ou `error` (ou `skipped`).

**Marqueur `X-PST-Import-Id`.** Chaque mail importé porte l'en-tête `X-PST-Import-Id: <pst_sha-item_key>`.
La valeur est délimitée par `<` et `>` : aucun identifiant n'est préfixe d'un autre. À la reprise :

- les éléments `ok` sont sautés ;
- les éléments `inflight` (et les `error` déjà tentés) sont d'abord recherchés sur le serveur
  (`UID SEARCH HEADER X-PST-Import-Id`, puis relecture de l'en-tête et **comparaison exacte**) ; le mail
  n'est renvoyé que s'il est absent. Une erreur temporaire pendant un APPEND (coupure après envoi) suit
  la même règle : reconnexion, recherche exacte, renvoi seulement si absent ;
- côté DAV, les UID iCal/vCard sont déterministes et le `PUT` écrase la ressource existante.

Un job n'est jamais `done` s'il reste des éléments `pending` ou `inflight`.

**Renvoi du même PST.** Les éléments sont rattachés à l'empreinte du PST (`pst_sha`, calculée à partir
des SHA-256 de ses chunks et de sa taille), pas à son identifiant interne. Supprimer un PST puis renvoyer
le même fichier retrouve donc l'état déjà migré, sans doublons. **Cette empreinte dépend de
`UPLOAD_CHUNK_SIZE`** : avec une autre taille de chunk, le même fichier a une autre empreinte et serait
traité comme un nouveau PST. Gardez la valeur constante pendant toute la migration d'un lot.

**Pause, arrêt.** Les APPEND non commencés sont annulés (retour à `pending`), ceux en cours vont à terme.

**Redémarrage ou arrêt brutal du worker.** À `SIGTERM`, les sous-processus reçoivent SIGTERM
(SIGKILL après `CHILD_TERM_GRACE` s) et les jobs repassent en file. Un job resté `running` alors que son
worker est mort (kill, crash, redémarrage du conteneur, heartbeat plus vieux que `WORKER_STALE_SECONDS`)
est remis en file automatiquement au démarrage du worker puis toutes les 15 s. Un arrêt brutal n'est
pas compté comme un crash.

**Isolation des crashs libpff.** Chaque job et chaque analyse de PST tourne dans un sous-processus.
Avant de lire un élément, il note dans la base l'élément en cours. S'il meurt (SIGSEGV, code ≠ 0) :

- l'élément fautif est marqué `error` « crash libpff » (jamais lu à nouveau, sauf par « relancer les
  erreurs ») ; un crash pendant l'itération saute l'index concerné ;
- le job est remis en file et reprend après cet élément ;
- après `MAX_CHILD_CRASHES` crashs (défaut 3) du même job, il passe en `failed`
  (« Le sous-processus a planté N fois ») ;
- un crash pendant l'analyse rend le PST `invalid` (« crash libpff pendant l'analyse »).

**Relancer les erreurs.** `POST /api/jobs/{id}/retry-errors` ne retraite que les éléments `error`
(avec vérification préalable sur le serveur pour ne pas dupliquer). « Relancer » (`retry`) refait un
passage complet, en sautant ce qui est déjà `ok`.

## Fonctionnement général

- **Authentification.** Un seul mot de passe, `ADMIN_PASSWORD`. La session est un cookie signé (HttpOnly,
  SameSite=Lax). Les mots de passe des boîtes sont chiffrés en Fernet (clé dérivée de `SECRET_KEY`).
- **Upload.** Le fichier est pré-alloué à sa taille finale (`fallocate`). Chaque chunk est lu en streaming,
  écrit à son offset, puis son SHA-256 est comparé à l'en-tête `X-Chunk-Sha256` (422 si différent). La
  liste des chunks reçus est en base. `complete` répond 409 s'il manque des chunks.
- **Dossiers IMAP.** Noms en UTF-7 modifié, séparateur du serveur, préfixe NAMESPACE respecté. Envoyés,
  Brouillons, Corbeille et Indésirables sont rattachés aux dossiers SPECIAL-USE du serveur (ou trouvés
  par leur nom). Avec `root_folder`, tout le PST est importé dessous, sans correspondance spéciale.
- **Envoi.** APPEND en parallèle sur N connexions (`workers` du serveur), reconnexion et backoff
  exponentiel sur erreur temporaire (`IMAP_MAX_RETRIES`). Les erreurs définitives sont enregistrées par
  élément. Un dépassement de quota fait échouer le job.
- **Vérifications.** Après chaque dossier, `STATUS MESSAGES` doit être au moins égal au nombre de mails
  envoyés. En fin de job, `VERIFY_SAMPLE` mails sont relus par en-tête et comparés par hash. Les
  ressources DAV sont comptées.
- **Temps réel.** `GET /api/events` (SSE) relaie les changements, détectés par des triggers SQLite
  (le worker est un autre process).

### DAV

Champs `caldav_url` / `carddav_url` du serveur :

- vide : auto-découverte via `https://<hôte IMAP>/.well-known/caldav|carddav`, puis via le domaine
  de l'adresse ;
- sinon : URL gabarit (`{email}`, `{user}` = partie locale, `{domain}`, `{login}`). Elle peut
  désigner directement une collection, ou un point d'entrée d'où l'on remonte au principal puis au
  home-set.

Le premier calendrier VEVENT ou carnet d'adresses trouvé est utilisé. S'il n'y en a aucun, une
collection « Importé PST » est créée (MKCALENDAR ou MKCOL étendu).

## Derrière un reverse proxy

Le service `web` écoute en HTTP simple sur 22362 et fait confiance aux en-têtes `X-Forwarded-*`
(uvicorn `proxy_headers`, tous proxys acceptés) : l'IP du client pour la limitation des connexions et le
schéma HTTPS pour le cookie. **Ne l'exposez donc pas directement à Internet en plus du proxy** : filtrez
le port 22362 (par exemple `127.0.0.1:22362:22362` dans le compose) pour que seul le proxy l'atteigne.

Points indispensables :

- **Pas de mise en tampon des requêtes** : les chunks de 32 Mio sont diffusés vers l'application ;
  sinon le proxy les stocke d'abord sur disque (lent, et espace consommé).
- **Pas de mise en tampon des réponses** pour `/api/events` (SSE), sinon les mises à jour n'arrivent pas.
- **Taille max du corps** supérieure ou égale à `UPLOAD_CHUNK_SIZE` (32 Mio par défaut : 64 Mio laisse
  de la marge).
- **Délais généreux** (ex. 3600 s) : le flux SSE reste ouvert, et l'aperçu ou un chunk lent peuvent durer.
- **`COOKIE_SECURE=1`** en HTTPS, et transmettre `X-Forwarded-For` / `X-Forwarded-Proto` / `Host`.

Exemple nginx :

```nginx
server {
    listen 443 ssl http2;
    server_name migration.exemple.fr;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    client_max_body_size 64m;          # >= UPLOAD_CHUNK_SIZE
    proxy_request_buffering off;       # chunks diffusés, pas stockés
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/events {             # SSE
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

Caddy : `reverse_proxy 127.0.0.1:22362` convient presque tel quel (HTTPS automatique, pas de limite de
corps par défaut, en-têtes `X-Forwarded-*` ajoutés). Pour le SSE, ajouter `flush_interval -1` dans le bloc
`reverse_proxy`, et ne pas réduire les délais.
Traefik : aucune limite de corps par défaut (ne pas activer le middleware `buffering` avec
`maxRequestBodyBytes` trop bas ni de `memRequestBodyBytes` qui met en tampon), augmenter si besoin les
`respondingTimeouts` (`readTimeout`, `writeTimeout`, `idleTimeout`) de l'entrypoint, et transmettre les
en-têtes `X-Forwarded-*` (activé par défaut).

## Limites connues

- **Un seul administrateur** : un mot de passe partagé, pas de comptes ni de rôles.
- **SQLite** (WAL) sur le volume `./data` : un seul hôte, pas de haute disponibilité. Le web et le worker
  partagent le même fichier, qui doit rester sur un disque local (pas de NFS).
- **Empreinte du PST liée à `UPLOAD_CHUNK_SIZE`** : changer cette valeur après un premier import fait
  perdre la reconnaissance du même fichier (voir [reprises](#fonctionnement-des-reprises)), avec un risque
  de doublons si le PST est renvoyé.
- **libpff** : un PST corrompu peut faire planter la bibliothèque native. Les éléments concernés sont
  ignorés (erreur « crash libpff ») ; au-delà de `MAX_CHILD_CRASHES` crashs, le job échoue. Le compteur
  est **par job** et remis à zéro à la fin du job. Un PST très abîmé peut être rejeté à l'analyse.
- **PST chiffrés / protégés par mot de passe** : aucune gestion dédiée dans le code ; un PST dont le
  contenu est chiffré donnera des erreurs ou sera `invalid`. Ne pas compter dessus.
- **Dépend de la qualité du serveur cible** : APPEND, SPECIAL-USE, quota (`GETQUOTAROOT` si disponible)
  et découverte DAV varient selon les serveurs.
- **Pas d'interface Swagger** : `/docs`, `/redoc` et `/openapi.json` sont désactivés (la référence de
  l'API est dans `docs/SPEC.md`).
- **Limitation de connexions en mémoire** : 5 échecs / 5 min par IP, remis à zéro au redémarrage de `web`.
  Derrière un proxy, vérifiez que `X-Forwarded-For` est bien transmis, sinon toutes les requêtes
  partagent l'IP du proxy.
- **Les mots de passe de boîtes sont stockés chiffrés** mais déchiffrables par l'application : protégez
  `./data` et la `SECRET_KEY`.
- **Interface en français uniquement.** Les libs JS sont vendorisées (fonctionne hors ligne), mais le
  navigateur doit supporter les API modernes (fetch, SSE, `File.slice`).

## Tests

Les tests tournent dans des conteneurs : GreenMail (IMAP), Radicale (CalDAV/CardDAV) et l'image de
l'application avec pytest (suites engine, server et e2e). Ils utilisent un faux engine
(`tests/server/fake_engine.py`) qui respecte le contrat ENGINE pour les tests serveur.

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v      # nettoyage
```

Test unitaire du code web (envoi par chunks), sans dépendance à installer :

```bash
docker run --rm -v "$PWD":/w -w /w node:22-slim node --test 'tests/web/*.test.mjs'
```

Couverture : CRUD, authentification, SSE, import CSV (`;` et `,`, entête optionnel, UTF-8, cp1252, BOM),
upload reprenable (chunk au hash faux → 422, reprise, fichier final identique, contrôle de l'espace
disque), import complet, dossier racine, pause et reprise, arrêt puis relance sans doublon, crash
simulé avec des éléments `inflight` et un job orphelin, crash libpff simulé (`PSTMIG_TEST_CRASH`),
relance des seules erreurs. Sans `TEST_IMAP_HOST` ni `TEST_DAV_URL`, les tests d'intégration sont ignorés.
