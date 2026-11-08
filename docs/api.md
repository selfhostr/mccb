# API

JSON over HTTP, `/api` prefix, session cookie or `Authorization: Bearer <API token>`. `/docs`, `/redoc` and `/openapi.json` are disabled.

- `POST /api/login {password}` ; `POST /api/logout` ; `GET /api/me`
- `POST /api/password {current, new}`, change the admin password (`new` ≥ 8 characters). Requires a valid session and a correct
  `current`. Returns **409** if the `ADMIN_PASSWORD` env var is set (the env var always wins, so the password cannot be changed
  through the API). On success, the new hash is stored in `data/admin.json`, **all other sessions are invalidated** and a
  fresh session cookie is returned for the caller.
- Servers: `GET/POST /api/servers`, `GET/PUT/DELETE /api/servers/{id}`, `POST /api/servers/{id}/test`
  Server = `{id,name,imap_host,imap_port,imap_security:'ssl'|'starttls'|'none',verify_tls,`
  `last_test:float|null,last_test_result:{imap,caldav,carddav}|null,` (stored by the test, cleared when a connection setting changes)
  `root_folder:str|null,caldav_url:str|null,carddav_url:str|null,dav_enabled:bool,workers:int,notes,`
  `tuning:'auto'|'manual',builders:int|null}` (manual: `workers` IMAP connections, `builders` builder processes, null =
  machine default; `tuning` omitted on creation = `manual` if `workers` is not 2 or 4 (earlier defaults) else `auto`, omitted on update = unchanged)
  (`caldav_url`/`carddav_url`: empty = `.well-known` auto-discovery, otherwise a template with `{email}`, `{user}`, `{domain}`)
- Mailboxes (`account` in the API): `GET /api/accounts?server_id=`, `POST /api/accounts {server_id,email,password,login?}`,
  `PUT/DELETE /api/accounts/{id}`, `POST /api/accounts/{id}/test` → `{imap:{ok,msg,quota},caldav:{ok,msg},carddav:{ok,msg}}`,
  `POST /api/accounts/import-csv` (multipart file + server_id; columns `email;password[;login]`, `,` or `;`, optional header)
  → `{created,updated,errors:[{line,msg}]}` ; `GET /api/accounts/csv-template`.
  Account = `{id,server_id,email,login,has_password,status:'new'|'ok'|'auth_failed'|'error',last_test,`
  `pst_id|null,source:{kind,config,has_secret},target:{kind|null,config,has_secret},options:{mail:bool,calendar:bool,contacts:bool},job:{...}|null}`
- Repair (see [Repair a damaged mailbox](repair.md)), a list separate from the accounts:
  `GET/POST /api/repair/boxes {server_id,email,password,login?}`, `PUT/DELETE /api/repair/boxes/{id}` (409 while a run is active),
  `POST /api/repair/boxes/{id}/test` → `{imap:{ok,msg,quota,uidplus}}`, `POST /api/repair/boxes/import-csv` (multipart file + server_id)
  → `{created,updated,errors,ids}`. Box = `{id,server_id,email,login,has_password,status,last_test,last_test_result,last_scan:Run|null,active_run:Run|null}`.
  `POST /api/repair/runs {box_ids,mode:'scan'|'repair'|'restore',folder?}` → 202 `{runs:[Run],skipped_busy:[id]}`; `GET /api/repair/runs/{id}`
  (with `report:{issues,folders,examples,errors,notes}`), `GET /api/repair/boxes/{id}/runs`, `POST /api/repair/runs/{id}/stop`.
  Run = `{id,box_id,mode,status:'queued'|'running'|'stopping'|'stopped'|'done'|'done_with_errors'|'failed',folder,progress:{total,seen,affected,repaired,restored,skipped,errors,folder,folders_done,folders_total},error,issues}`.
  `POST /api/repair/boxes/{id}/preview {folder,uid}` → `{issues,already,before:{subject,text,parts},after|null,fixes,left,error}` (reads only).
- Connectors (see [Connectors](connectors.md)):
  `GET /api/connectors` → `[{kind,label,description,source:Role|null,target:Role|null}]`, with
  Role = `{items:[...],capabilities:[...],fields:[{name,label,type,required,secret,default,options?,help?}]}`.
  Render forms from it: `type` is `text`, `password`, `number`, `boolean`, `select` or `url`, and new kinds need no UI change.
  `GET/PUT /api/accounts/{id}/source`, `PUT` body `{kind,config,secrets?}`, returns the account. `kind:"pst"` goes back to the PST
  (`config:{pst_id}` optional). Migration state is kept per source: pointing at a source again resumes it without duplicates.
  `PUT /api/accounts/{id}/pst` only changes the linked file: it does not switch an account away from another source, except between a PST (`kind:"pst"`) and any other uploaded file (`kind:"files"`, `config:{pst_id}`), which it sets itself.
  `GET/PUT /api/accounts/{id}/target`, same body, `kind:null` goes back to the server of the account. The target serves the items
  it declares, the server covers the others. Pointing at another mailbox forgets the account's items: everything is sent again.
  Secrets: `secrets` omitted keeps them, `{}` clears them, a value replaces that field. They are never returned (`has_secret` only).
  **422** invalid config or unknown kind, **409** while a job is active for the account.
  `POST /api/connectors/{kind}/test {role:'source'|'target',config,secrets?,account_id?}` tries a config without saving it
  (`secrets` omitted with `account_id`: those stored on the account for this kind) →
  `{ok:true,msg,folders:[{path,kind,special,count}]}` for a source, `{ok:true,msg,quota,...}` for a target,
  `{ok:false,error_kind:'auth'|'config'|'unreachable'|'transient'|'rejected'|'quota'|'folder_missing',msg}` when refused.
  Exchange kinds and their fields: see [Exchange](exchange.md). A job's `progress` and report gain `tasks` on routes that carry tasks.
  `POST /api/accounts/{id}/rules {delimiter?,root_folder?,trash_folder?,managesieve?:{host?,port?,security?,verify_tls?,login?,password?,script_name?,activate?}}`
  reads the inbox rules of a source with the `rules` capability and converts them to Sieve, best effort →
  `{ok:true,script,converted:[name],partial:[{name,dropped:[...]}],not_converted:[{name,reasons:[...]}],warnings:[...],uploaded,activated,upload_error:{error_kind,msg}|null}`,
  `{ok:false,error_kind,msg}` if the source refuses. `managesieve` uploads the script (default host, login and password: those of the account);
  it is not activated unless `activate` is true. **422** if the source cannot read rules.
  Fields gain `scope:'mailbox'|'connection'` and connectors `directory:bool` (true: they can be a source connection).
  An account source gains `connection:{id,name}|null` (absent for a PST); `has_secret` is then that of the connection.
  `PUT /api/accounts/{id}/source {connection_id,config}` reads the source through that connection: `config` carries the
  mailbox fields only (others **422**, so do `secrets`); a `PUT` without `connection_id` drops the link.
  The CSV export writes the merged settings of a linked account (never the secret); importing them back keeps the link,
  other settings replace it.
- Source connections (one credential for many accounts, v1: a Microsoft 365 tenant, see [Exchange](exchange.md)):
  `GET /api/source-connections` → `[Connection]`, `POST` `{name,kind,config,secrets,notes?}` → 201 Connection,
  `GET/PUT/DELETE /api/source-connections/{id}`. Connection = `{id,name,kind,config,has_secret,notes,last_test,last_test_result,accounts,created_at}`
  (`accounts`: linked count, never a secret). `config` holds the kind's `connection` fields only (others **422**), the kind
  needs a directory. `PUT`: fields omitted are kept, `secrets` as for accounts; changing a connection field or the secret
  clears the last test. **409** when changing `tenant_id` or `cloud`, or deleting, while accounts are linked
  (`detail:{msg,accounts}` on delete).
  `POST /api/source-connections/test {kind,config,secrets?,id?}` (unsaved; `id` fills omitted secrets) and
  `POST /api/source-connections/{id}/test` (stored, saves the result) → `{ok,checks:[{key,ok,level:'required'|'optional',msg,missing?}],tenant_name?}`;
  a refusal is an answer, not an HTTP error.
  `GET /api/source-connections/{id}/mailboxes?refresh=0|1` → `{fetched_at,purpose_checked,mailboxes:[{id,display_name,mail,upn,aliases,kind,enabled,licensed,guess?}],hidden:{guests,no_mailbox}}`,
  kept 5 minutes in memory (`refresh=1` reads again); **502** when the tenant refuses.
  `GET /api/source-connections/{id}/match?server_id=N&refresh=0|1` (no `server_id`: the server with the most matches) →
  `{connection:{id,name},server:{id,name,dav_enabled,caldav_ok,carddav_ok},rows,counts,fetched_at,purpose_checked,hidden}`,
  row = `{state:'ready'|'linked'|'no_target'|'no_source'|'other_source'|'conflict',mailbox|null,account:{id,email,status,last_test,last_test_result,source_kind,source_connection_id}|null,`
  `matched_by:'mail'|'upn'|'alias'|null,matched_address|null,conflict_with?,coverage:{mail,calendar,contacts,tasks},last_job:{id,status}|null}`,
  coverage values `on|off|no_dav|failed|unknown|no_permission`.
  `POST /api/source-connections/{id}/link {server_id,links:[{account_id,mailbox_id}],include_tasks?,start?,mode?,batch_name?}`
  → `{linked:[account_id],errors:[{account_id,msg}],jobs:[Job],batch_id}`. Per account: on that server, no active job, no
  other source (a PST without a valid file is none); others go on. An account already linked, or configured alone for the same
  tenant, keeps its `mailbox` value: same migration state, no duplicates. `include_tasks` omitted: the last test's tasks
  check (true if never tested). `start` queues the jobs as `POST /api/runs` does. **409** `detail:{msg,errors}` when none is linked.
- PST upload (resumable):
  `POST /api/uploads {filename,size,last_modified}` → `{id,chunk_size,received:[n...]}` (same file = same upload, resume)
  `PUT /api/uploads/{id}/chunks/{n}` binary body, `X-Chunk-Sha256` header → 200 `{n,ok}` / 422 if wrong hash
  `GET /api/uploads/{id}` → `{id,filename,size,chunk_size,received:[...],status}`
  `POST /api/uploads/{id}/complete` → starts validation (signature + scan); `GET /api/psts`, `GET /api/psts/{id}`,
  `DELETE /api/psts/{id}` ; `GET /api/psts/{id}/preview?limit=20` (limit 1..200) → zip (.eml/.ics/.vcf per folder, sample spread across mails/appointments/contacts) generated by the engine in a subprocess (`app.server.preview`, 120 s timeout); 404 unknown, 409 if PST not `valid`, 500 `{detail:"crash libpff pendant l'aperçu"}` if the subprocess dies; PST = `{id,filename,size,status:'uploading'|'validating'|'valid'|'invalid',scan:{...},error}`. An uploaded file that is not a PST (mbox, Maildir in a zip, .eml, .msg, .ics, .vcf, .zip) is analysed too: `scan = {format:'files',contents:[...],folders:[{path,kind,count}],totals:{mail,calendar,contacts},skipped:[...],n_skipped}`, and `/preview` answers 409 for it. Validation errors name both the PST and the file formats
  `PUT /api/accounts/{id}/pst {pst_id}`
- Server-side intake (see [Server-side intake](server-intake.md)):
  `POST /api/psts/fetch` starts a download on the server and returns the PST row (status `fetching`). Body, by `kind`:
  `http` `{url, token? | username?, password?}`; `sftp` `{host, port?, path, username, password? | private_key?, passphrase?,
  host_key_sha256?}`; `s3` `{endpoint, bucket, key, region?, access_key, secret_key}`; all accept `filename?`.
  422 if the destination is refused (link-local, metadata) or the body is incomplete, 507 if the disk is too full.
  Secrets are encrypted and never returned.
  `POST /api/psts/{id}/cancel` → `{ok, state:'cancelling'|'removed'}` (409 if the PST is not being fetched);
  `POST /api/psts/{id}/retry-fetch` (only from `fetch_failed`) → PST.
  `GET /api/intake/inbox` → `{path, enabled, settle_seconds, files:[{name,size,modified_ago}]}`.
  PST rows gain `source:'upload'|'inbox'|'fetch'`, `origin` (label without secrets), `received_bytes` (progress while
  `fetching`; `size` is 0 until the total is known) and `chain` (outcome of the automatic assignment, or null:
  `{result:'assigned'|'ambiguous'|'no_match'|'account_has_pst'|'already_assigned'|'error', account_id?, job_id?, ...}`).
  PST `status` also takes `fetching`, `cancelling` and `fetch_failed` (reason in `error`).
- Settings: `GET /api/intake/settings`, `PUT /api/intake/settings` (partial update, same shape) →
  `{notifications:{enabled,mask_addresses,group_window,disk_low_mb,events:{<event>:bool},`
  `ntfy:{enabled,url,topic,priority,has_token},webhook:{enabled,url,has_token},`
  `email:{enabled,host,port,security,username,from,to,has_password}},`
  `inbox:{enabled,settle_seconds,path},auto_chain:{enabled,start_import,default_domains:{server_id:domain}}}`.
  Secrets (`ntfy.token`, `webhook.token`, `email.password`) are write-only: omit to keep, `""` to clear. 422 with a message on invalid input.
  `POST /api/intake/notifications/test {channel?}` → `{ok, results:{<channel>:{ok,error}}}`; no `channel` = every enabled one.
- Jobs: `POST /api/runs {account_ids:[...]}` (queues), `POST /api/jobs/{id}/pause|resume|stop|retry`,
  `GET /api/jobs?active=1`, `GET /api/jobs/{id}` → `{id,account_id,status:'queued'|'running'|'paused'|'stopping'|'stopped'|'done'|'done_with_errors'|'failed',`
  `phase:'precheck'|'mail'|'calendar'|'contacts'|'verify',progress:{mail:{done,total,errors},calendar:{...},contacts:{...}},`
  `current_folder,rate,eta,started_at,finished_at,error,tuning:{mode,builders,connections,fallback}|null}`
  `GET /api/jobs/{id}/log?after=` (lines), `GET /api/jobs/{id}/report` (JSON), `GET /api/jobs/{id}/errors`,
  `POST /api/jobs/{id}/retry-errors`
- Planning (see [Plan, filter and control a migration](planning.md)):
  `POST /api/runs/plan {account_ids}` → **202** `{id,status:'queued'|'running'|'done'|'failed',account_ids,error,result}`; `GET /api/plans/{id}` (full result:
  `{accounts:[{account_id,email,status:'ok'|'warning'|'error',source,targets,kinds,folders,quota,target_limit,warnings,notes}],totals,done,total}`), `GET /api/plans`.
  Nothing is written to a target. `POST /api/runs` also takes `name` (creates a batch), `mode:'full'|'delta'` and `priority`.
  `GET/PUT /api/servers/{id}/policy` and `GET/PUT /api/accounts/{id}/policy`: `{policy}` (account: also `effective`); body
  `{folder_map:[{source,target}],exclude_folders:[...],date_from,date_to,max_size_mb}` plus, for a server, `window:{days?,start,end,tz}` and `rate_mb_s`;
  `null` or `{}` clears; **422** with the reason when invalid.
  `GET /api/batches`, `GET /api/batches/{id}` → `{id,name,created_at,state:'queued'|'running'|'paused'|'finished',jobs,by_status,items,failed,with_errors,finished_at,job_list?}`;
  `POST /api/batches/{id}/pause|resume|stop|retry-failed` → batch plus `{changed,skipped}`.
  `POST /api/jobs/reorder {job_ids}` → `{queue:[...]}`; `PUT /api/jobs/{id}/priority {priority}`; `POST /api/jobs/{id}/delta` (final resync of a finished job).
  `POST /api/jobs/{id}/compare {detail?}` → **202**; `GET /api/jobs/{id}/compare[?run=1&detail=1]` → `{id,status,result:{ok,totals,folders:[{kind,target,folders,expected,on_target,missing,extra,sent,errors,pending,filtered,skipped,missing_items?}]}}` or `{status:'none'}`;
  `GET /api/jobs/{id}/compare/csv`. `GET /api/jobs/{id}/filtered`, `GET /api/jobs/{id}/oversized`.
  Jobs gain `priority`, `batch_id` and `pause_reason` (`'window'`: waiting for the allowed hours, resumes by itself; `null`: paused by the administrator).
  The report gains `filtered`, `oversized`, `policy`, `totals.<kind>.filtered` and, after a comparison, `compare`.
- Mailboxes as CSV: `POST /api/accounts/import-csv` accepts the optional columns of [Import mailboxes](import-accounts.md) and may answer `warnings:[{line,msg}]`;
  `GET /api/accounts/export-csv[?server_id=]` writes them back (never a password or a secret).
- API tokens (see [Command line and API tokens](cli.md)): `GET /api/tokens`, `POST /api/tokens {name}` → **201** with `token` (shown once), `DELETE /api/tokens/{id}`.
  Any route accepts `Authorization: Bearer <token>`; the token routes themselves need a session (**403** with a token).
- Real time: `GET /api/events` (SSE) emits `job` / `pst` / `account` with the full object, `plan` and `compare` (plans without their folder lists), and `upload` with `{id,status,received_count}`.
- `GET /api/health`.
