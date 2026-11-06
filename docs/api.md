# API

JSON over HTTP, `/api` prefix, session cookie. `/docs`, `/redoc` and `/openapi.json` are disabled.

- `POST /api/login {password}` ; `POST /api/logout` ; `GET /api/me`
- `POST /api/password {current, new}`, change the admin password (`new` ≥ 8 characters). Requires a valid session and a correct
  `current`. Returns **409** if the `ADMIN_PASSWORD` env var is set (the env var always wins, so the password cannot be changed
  through the API). On success, the new hash is stored in `data/admin.json`, **all other sessions are invalidated** and a
  fresh session cookie is returned for the caller.
- Servers: `GET/POST /api/servers`, `GET/PUT/DELETE /api/servers/{id}`, `POST /api/servers/{id}/test`
  Server = `{id,name,imap_host,imap_port,imap_security:'ssl'|'starttls'|'none',verify_tls,`
  `root_folder:str|null,caldav_url:str|null,carddav_url:str|null,dav_enabled:bool,workers:int,notes,`
  `tuning:'auto'|'manual',builders:int|null}` (manual: `workers` IMAP connections, `builders` builder processes, null =
  machine default; `tuning` omitted on creation = `manual` if `workers` is not 2 or 4 (earlier defaults) else `auto`, omitted on update = unchanged)
  (`caldav_url`/`carddav_url`: empty = `.well-known` auto-discovery, otherwise a template with `{email}`, `{user}`, `{domain}`)
- Accounts: `GET /api/accounts?server_id=`, `POST /api/accounts {server_id,email,password,login?}`,
  `PUT/DELETE /api/accounts/{id}`, `POST /api/accounts/{id}/test` → `{imap:{ok,msg,quota},caldav:{ok,msg},carddav:{ok,msg}}`,
  `POST /api/accounts/import-csv` (multipart file + server_id; columns `email;password[;login]`, `,` or `;`, optional header)
  → `{created,updated,errors:[{line,msg}]}` ; `GET /api/accounts/csv-template`.
  Account = `{id,server_id,email,login,has_password,status:'new'|'ok'|'auth_failed'|'error',last_test,`
  `pst_id|null,source:{kind,config,has_secret},target:{kind|null,config,has_secret},options:{mail:bool,calendar:bool,contacts:bool},job:{...}|null}`
- Connectors (see [Connectors](connectors.md)):
  `GET /api/connectors` → `[{kind,label,description,source:Role|null,target:Role|null}]`, with
  Role = `{items:[...],capabilities:[...],fields:[{name,label,type,required,secret,default,options?,help?}]}`.
  Render forms from it: `type` is `text`, `password`, `number`, `boolean`, `select` or `url`, and new kinds need no UI change.
  `GET/PUT /api/accounts/{id}/source`, `PUT` body `{kind,config,secrets?}`, returns the account. `kind:"pst"` goes back to the PST
  (`config:{pst_id}` optional). Migration state is kept per source: pointing at a source again resumes it without duplicates.
  `PUT /api/accounts/{id}/pst` only changes the linked PST: it does not switch an account away from another source.
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
- PST upload (resumable):
  `POST /api/uploads {filename,size,last_modified}` → `{id,chunk_size,received:[n...]}` (same file = same upload, resume)
  `PUT /api/uploads/{id}/chunks/{n}` binary body, `X-Chunk-Sha256` header → 200 / 422 if wrong hash
  `GET /api/uploads/{id}` → `{id,filename,size,chunk_size,received:[...],status}`
  `POST /api/uploads/{id}/complete` → starts validation (signature + scan); `GET /api/psts`, `GET /api/psts/{id}`,
  `DELETE /api/psts/{id}` ; `GET /api/psts/{id}/preview?limit=20` (limit 1..200) → zip (.eml/.ics/.vcf per folder, sample spread across mails/appointments/contacts) generated by the engine in a subprocess (`app.server.preview`, 120 s timeout); 404 unknown, 409 if PST not `valid`, 500 `{detail:"crash libpff pendant l'aperçu"}` if the subprocess dies; PST = `{id,filename,size,status:'uploading'|'validating'|'valid'|'invalid',scan:{...},error}`
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
- Real time: `GET /api/events` (SSE) emits `job` / `upload` / `pst` / `account` with the full object.
- `GET /api/health`.
