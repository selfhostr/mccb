# PST → IMAP / CalDAV / CardDAV Migrator — Spécification

## But
Outil web (Docker, port **22362**) pour migrer en masse des clients POP/Outlook vers nos serveurs :
pour chaque boîte, on fournit l'adresse + mot de passe + le PST du client ; un « Run » importe
**mails (IMAP)**, **rendez-vous (CalDAV)**, **contacts (CardDAV)**, boîte par boîte, avec
vérifications partout, pause/arrêt/reprise sans doublons. UI en français, moderne.

## Anti-objectifs (échec même si « ça marche »)
- Accents cassés (U+FFFD, mojibake « Ã© ») dans sujet, corps texte, corps HTML, noms de pièces jointes, noms de dossiers.
- Pièces jointes renommées `piece_jointe_N`, en `application/octet-stream` alors que le type est connu, tronquées, ou images de signature affichées en pièce jointe au lieu d'être intégrées (cid).
- Doublons après une reprise. Plantage sur un PST de 50 Go. Upload de 50 Go qui échoue sans pouvoir reprendre.

## Leçons tirées de l'ancien script `mig.py` (à NE PAS reproduire)
1. `html_body` était décodé en UTF-8 alors qu'il est dans le codepage du message → U+FFFD.
   Correct : lire `PR_INTERNET_CPID` (0x3FDE) / `PR_MESSAGE_CODEPAGE` (0x3FFD), décoder avec ce codepage
   (fallback : charset du `<meta>` HTML, puis cp1252) ; les chaînes MAPI PT_UNICODE (0x001F) sont UTF-16-LE,
   PT_STRING8 (0x001E) sont dans le codepage. Émettre tout en UTF-8 (`charset=utf-8`, QP/base64).
2. Si pas de HTML ni texte : décompresser `PR_RTF_COMPRESSED` (0x1009) ; si RTF encapsule du HTML
   (`\fromhtml1`) → extraire le HTML ; sinon convertir RTF → texte.
3. `a.name` n'existe pas dans pypff → noms perdus. Correct : `PR_ATTACH_LONG_FILENAME` 0x3707,
   fallback `PR_ATTACH_FILENAME` 0x3704, `PR_DISPLAY_NAME` 0x3001 ; MIME `PR_ATTACH_MIME_TAG` 0x370E ;
   `PR_ATTACH_CONTENT_ID` 0x3712 ; `PR_ATTACH_FLAGS` 0x3714 / `PR_ATTACHMENT_HIDDEN` 0x7FFE ;
   `PR_ATTACH_METHOD` 0x3705 (5 = message embarqué → l'attacher en `message/rfc822` construit récursivement).
   Type manquant → détection par contenu (voir `detecte()` du script de réparation) + `mimetypes` par extension.
   Images référencées par `cid:` dans le HTML → `multipart/related` inline avec Content-ID.
   Noms de fichiers non-ASCII encodés RFC 2231 (géré par `email.message.EmailMessage`).
4. En-têtes de transport d'origine : les conserver (From, To, Cc, Date, Message-ID, In-Reply-To, References,
   Subject…) mais **décoder/ré-encoder proprement** (RFC 2047) ; s'ils manquent (mails POP sans en-têtes,
   mails envoyés), reconstruire depuis MAPI : sujet, expéditeur nom+adresse (`PR_SENDER_EMAIL_ADDRESS`,
   SMTP si EX via `PR_SENDER_SMTP_ADDRESS` 0x5D01), destinataires depuis la table des destinataires, dates.
5. Flags IMAP : `\Seen` si `PR_MESSAGE_FLAGS` & 1 ; `\Flagged` si suivi ; `\Answered` si répondu ; brouillons `\Draft`.
6. Dossiers : noms en UTF-7 modifié IMAP ; respecter le séparateur du serveur ; mapper les dossiers spéciaux
   (Boîte de réception→INBOX, Éléments envoyés→dossier \Sent du serveur via SPECIAL-USE, etc.) ; option
   « importer sous un dossier racine » (ex. `Import PST`). Dossiers non-mail (contacts, calendrier, tâches…)
   détectés par `PR_CONTAINER_CLASS` (IPF.Note / IPF.Contact / IPF.Appointment / IPF.Task…), pas par le nom.

## Vérifications obligatoires
- **Upload** : SHA-256 de chaque chunk vérifié côté serveur ; taille finale ; signature PST `!BDN` ;
  ouverture pypff + scan complet (comptage mails/contacts/rdv par dossier) → statut « PST valide ».
- **Compte** : login IMAP (test à l'ajout + avant run), accès DAV, quota (GETQUOTAROOT si dispo) vs taille estimée.
- **Chaque mail construit** : re-parse du MIME ; pas de U+FFFD ajouté par nous ; nb + taille des pièces jointes = source ;
  tout `cid:` du HTML résolu ; si la construction riche échoue → fallback simplifié **toujours en UTF-8 avec PJ nommées**, marqué warning.
- **Après chaque dossier** : `STATUS MESSAGES` serveur ≥ envoyés ; **après chaque boîte** : relecture d'un échantillon
  (N mails) par en-tête `X-PST-Import-Id` et comparaison du hash ; DAV : comptage des ressources.
- **Rapport** par boîte (JSON + affichage) : envoyés / présents / erreurs / avertissements / ignorés, téléchargeable.

## Idempotence / reprise
Chaque élément a une clé stable `item_key` = identifiant de nœud pypff (`msg.identifier`) + dossier (clé de repli
`<fnid>-x<index>` si l'élément est illisible à l'itération : à la relance, s'il devient lisible, il est traité sous sa
vraie clé et la ligne de repli est supprimée). Chaque mail porte l'en-tête `X-PST-Import-Id: <pst_sha-item_key>`
(valeur **délimitée par `<>`** : aucun id n'est préfixe d'un autre). Table `items` (account_id, **pst_key**, kind,
item_key, status pending|inflight|ok|error|skipped, error, …) : `pst_key` = `psts.pst_sha` (empreinte des blocs +
taille), **pas l'id du PST** — supprimer puis renvoyer le même fichier retrouve l'état (migration automatique des
anciennes lignes `pst_id` au démarrage ; `kv` `pst:<pst_sha>:crash_skips`). Clé de dossier : chemin joint par `/`,
`/` et `\` des noms échappés ; noms IMAP cibles uniques (suffixe déterministe ` (2)`, ` (3)`… en cas de collision
après nettoyage). Recherche d'un mail : `UID SEARCH HEADER X-PST-Import-Id <sha-clé>` (sous-chaîne) puis
`UID FETCH BODY.PEEK[HEADER.FIELDS (X-PST-Import-Id)]` et **comparaison exacte** (la forme historique non délimitée
est aussi reconnue). À la reprise : `ok` sautés ; `inflight` et `error` déjà tentés (hash connu) → recherche exacte
avant tout renvoi. Une erreur temporaire pendant un APPEND (délai, coupure après envoi du littéral) → reconnexion +
recherche exacte, renvoi seulement si absent. Pause/arrêt : les APPEND non commencés sont annulés (`pending`), ceux en
cours vont à terme, tous les threads sont joints. Un job n'est jamais `done` s'il reste des éléments
`pending`/`inflight` (→ `done_with_errors` « non terminé(s) »). Un CREATE refusé ne met en erreur que les éléments de
ce dossier. Contrôle « présents ≥ envoyés » : seuls nos messages comptent (SEARCH de `<pst_sha>-` par dossier ; DAV :
noms de ressources = nos UID). DAV : UID iCal/vCard déterministes → `PUT` idempotent.
**Crash natif (libpff)** : avant chaque lecture libpff d'un élément, le sous-processus du job commite un marqueur
« élément en cours » (`kv` `job:<id>:current` : dossier, index, item_key). Si le sous-processus meurt (signal, code ≠ 0),
le worker marque cet élément `error` « crash libpff » (jamais un `inflight`, toujours résolu par `UID SEARCH`) et remet
le job en file : la reprise saute l'élément (un crash pendant `iter_items` saute l'index, `kv` `pst:<id>:crash_skips`).
Les éléments « crash libpff » ne sont plus relus (sauf « relancer les erreurs », une fois). Après `MAX_CHILD_CRASHES`
(défaut 3) crashs du même job → `failed` « Le sous-processus a planté N fois (crash libpff probable) ». Un crash pendant
l'analyse → PST `invalid` « crash libpff pendant l'analyse ». Un arrêt brutal du conteneur n'est pas compté comme crash
(reprise habituelle par `recover()`). Crochet de test : `PSTMIG_TEST_CRASH` (voir `app/server/crash.py`).

## Architecture
- Un seul `docker-compose.yml`, une image, deux services : `web` (FastAPI/uvicorn, port 22362) et `worker`
  (superviseur des jobs). Le worker exécute **chaque job et chaque analyse de PST dans un sous-processus**
  (`python -m app.server.child job|validate`) : un SIGSEGV de libpff ne tue que ce sous-processus. Le parent garde le
  heartbeat ; pause/arrêt passent par la DB ; sur SIGTERM du worker, les sous-processus reçoivent SIGTERM (SIGKILL après
  `CHILD_TERM_GRACE` s) et les jobs repassent `queued`. Les sous-processus meurent avec le parent (PR_SET_PDEATHSIG). Volume `./data` : SQLite (WAL) `data/app.db`, PST `data/pst/`, rapports `data/reports/`, logs.
- Python 3.12, `libpff-python` (pypff), FastAPI, SQLAlchemy 2 (ou sqlite3 direct), `cryptography` (Fernet), `httpx`,
  `icalendar`/`vobject`. IMAP via `imaplib` (IMAP4_SSL / STARTTLS / clair configurables, vérif TLS désactivable par serveur).
- Auth dashboard : `ADMIN_PASSWORD` (env), cookie de session signé. Mots de passe des boîtes chiffrés (Fernet, clé `SECRET_KEY`
  env ou générée dans `data/secret.key`).
- Pas de build Node : front statique servi par FastAPI (`app/web/`), libs JS **vendorisées** (fonctionne hors ligne).

## Arborescence
```
docker-compose.yml  Dockerfile  .env.example  README.md
app/engine/   # Agent ENGINE : lecture PST → EML / vCard / ICS, vérifications (pur, sans réseau)
app/server/   # Agent SERVER : API, DB, upload, IMAP, DAV, runner/worker
app/web/      # Agent WEB    : SPA statique
tests/        # tests pytest ; tests/fixtures/ PST d'exemple ; docker-compose.test.yml (dovecot/greenmail + radicale)
```

## Contrat ENGINE (`app/engine/__init__.py` exporte exactement ceci)
```python
@dataclass
class FolderInfo:
    path: list[str]          # noms Unicode depuis la racine du PST (racine « Début du fichier… » exclue)
    kind: str                # 'mail' | 'contacts' | 'calendar' | 'tasks' | 'notes' | 'journal' | 'other'
    container_class: str     # ex. 'IPF.Note'
    special: str | None      # 'inbox'|'sent'|'drafts'|'trash'|'junk'|'outbox'|None
    count: int               # nb d'éléments
    _folder: object          # handle pypff

@dataclass
class BuiltMessage:
    raw: bytes               # RFC 5322 complet, UTF-8 propre, contient X-PST-Import-Id
    internaldate: datetime   # tz-aware
    flags: list[str]         # ex. ['\\Seen']
    subject: str
    attachments: int
    size: int
    warnings: list[str]      # ex. 'fallback', 'rtf-converted', 'cid-unresolved:image001.png'

@dataclass
class DavObject:
    uid: str                 # déterministe
    filename: str            # f"{uid}.ics" / f"{uid}.vcf"
    content: str             # VCALENDAR complet / VCARD 3.0
    summary: str

class PstFile:  # context manager
    def __init__(self, path: str): ...
    def folders(self) -> list[FolderInfo]: ...           # parcours complet, ordre stable
    def iter_items(self, folder: FolderInfo, start: int = 0) -> Iterator[tuple[int, str, object]]:
        """(index, item_key, handle) ; item_key stable entre deux ouvertures"""
    def build_message(self, handle, import_id: str) -> BuiltMessage | None   # None = pas un mail
    def build_contact(self, handle) -> DavObject | None
    def build_event(self, handle) -> DavObject | None    # rdv, récurrence si possible (RRULE), participants, rappel
    def scan(self) -> dict    # {'folders':[{path,kind,count}], 'totals':{'mail':n,'contacts':n,'calendar':n}, 'errors':[...]}

def verify_message(built: BuiltMessage, handle) -> list[str]   # erreurs bloquantes (vide = OK)
def check_pst_signature(path: str) -> bool
```
Exceptions propres, jamais de crash du process sur un élément corrompu (l'erreur remonte par élément).
Mémoire bornée : ne jamais charger tout le PST ; PJ lues par blocs si possible.

## Contrat API (SERVER ↔ WEB) — JSON, préfixe `/api`, cookie de session
- `POST /api/login {password}` ; `POST /api/logout` ; `GET /api/me`
- Serveurs : `GET/POST /api/servers`, `GET/PUT/DELETE /api/servers/{id}`, `POST /api/servers/{id}/test`
  Server = `{id,name,imap_host,imap_port,imap_security:'ssl'|'starttls'|'none',verify_tls,`
  `root_folder:str|null,caldav_url:str|null,carddav_url:str|null,dav_enabled:bool,workers:int,notes}`
  (`caldav_url`/`carddav_url` : vide = auto-découverte `.well-known`, sinon gabarit avec `{email}`, `{user}`, `{domain}`)
- Comptes : `GET /api/accounts?server_id=`, `POST /api/accounts {server_id,email,password,login?}`,
  `PUT/DELETE /api/accounts/{id}`, `POST /api/accounts/{id}/test` → `{imap:{ok,msg,quota},caldav:{ok,msg},carddav:{ok,msg}}`,
  `POST /api/accounts/import-csv` (multipart file + server_id ; colonnes `email;password[;login]`, `,` ou `;`, entête optionnel)
  → `{created,updated,errors:[{line,msg}]}` ; `GET /api/accounts/csv-template`.
  Account = `{id,server_id,email,login,has_password,status:'new'|'ok'|'auth_failed'|'error',last_test,`
  `pst_id|null,options:{mail:bool,calendar:bool,contacts:bool},job:{...}|null}`
- Upload PST (reprenable) :
  `POST /api/uploads {filename,size,last_modified}` → `{id,chunk_size,received:[n...]}` (même fichier = même upload, reprise)
  `PUT /api/uploads/{id}/chunks/{n}` corps binaire, en-tête `X-Chunk-Sha256` → 200 / 422 si hash faux
  `GET /api/uploads/{id}` → `{id,filename,size,chunk_size,received:[...],status}`
  `POST /api/uploads/{id}/complete` → lance validation (signature + scan) ; `GET /api/psts`, `GET /api/psts/{id}`,
  `DELETE /api/psts/{id}` ; `GET /api/psts/{id}/preview?limit=20` (limit 1..200) → zip (.eml/.ics/.vcf par dossier, échantillon réparti mails/rdv/contacts) généré par l'engine dans un sous-processus (`app.server.preview`, timeout 120 s) ; 404 inconnu, 409 si PST non `valid`, 500 `{detail:"crash libpff pendant l'aperçu"}` si le sous-processus meurt ; PST = `{id,filename,size,status:'uploading'|'validating'|'valid'|'invalid',scan:{...},error}`
  `PUT /api/accounts/{id}/pst {pst_id}`
- Jobs : `POST /api/runs {account_ids:[...]}` (met en file), `POST /api/jobs/{id}/pause|resume|stop|retry`,
  `GET /api/jobs?active=1`, `GET /api/jobs/{id}` → `{id,account_id,status:'queued'|'running'|'paused'|'stopping'|'stopped'|'done'|'done_with_errors'|'failed',`
  `phase:'precheck'|'mail'|'calendar'|'contacts'|'verify',progress:{mail:{done,total,errors},calendar:{...},contacts:{...}},`
  `current_folder,rate,eta,started_at,finished_at,error}`
  `GET /api/jobs/{id}/log?after=` (lignes), `GET /api/jobs/{id}/report` (JSON), `GET /api/jobs/{id}/errors`,
  `POST /api/jobs/{id}/retry-errors`
- Temps réel : `GET /api/events` (SSE) émet `job` / `upload` / `pst` / `account` avec l'objet complet.
- `GET /api/health`.

## UI (français)
Pages : Tableau de bord (jobs en cours, stats globales) · Serveurs · Comptes (table : email, état test 🟢/🔴,
PST associé + statut, options mail/agenda/contacts, progression, actions) · Uploads · Détail job (progression par phase,
dossier courant, débit, ETA, journal en direct, erreurs, rapport). Sélection multiple + « Lancer ». Drag-and-drop PST.
Upload reprenable après rafraîchissement (clé `filename+size+lastModified`), 3 chunks en parallèle, retry auto.
