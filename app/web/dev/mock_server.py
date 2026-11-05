#!/usr/bin/env python3
"""Faux backend de développement pour app/web (stdlib uniquement).
Usage : python3 dev/mock_server.py [port]   -- mot de passe : admin
Env : MOCK_CHUNK_DELAY (s, défaut 0), MOCK_FAIL_EVERY (n : 1 chunk sur n répond 503, défaut 0)."""
import hashlib, json, mimetypes, os, queue, random, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 8 * 1024 * 1024
DELAY = float(os.environ.get("MOCK_CHUNK_DELAY", "0"))
FAIL_EVERY = int(os.environ.get("MOCK_FAIL_EVERY", "0"))
LOCK = threading.RLock()
SESSIONS, CLIENTS = set(), []
ID = {"server": 3, "account": 1, "pst": 1, "job": 1}
chunk_counter = 0

def nid(k):
    with LOCK:
        ID[k] += 1
        return ID[k]

servers = {
    1: dict(id=1, name="Production OVH", imap_host="imap.exemple.fr", imap_port=993, imap_security="ssl", verify_tls=True, root_folder="Import PST",
            caldav_url="https://dav.exemple.fr/{email}/calendar/", carddav_url="https://dav.exemple.fr/{email}/contacts/", dav_enabled=True, workers=2, notes="Serveur principal"),
    2: dict(id=2, name="Recette", imap_host="10.0.0.5", imap_port=143, imap_security="starttls", verify_tls=False, root_folder=None,
            caldav_url=None, carddav_url=None, dav_enabled=False, workers=1, notes=""),
    3: dict(id=3, name="Dovecot local", imap_host="localhost", imap_port=993, imap_security="ssl", verify_tls=False, root_folder=None,
            caldav_url=None, carddav_url=None, dav_enabled=True, workers=1, notes="")}
accounts, uploads, psts, jobs, logs = {}, {}, {}, {}, {}
for i, (e, sid) in enumerate([("alice@exemple.fr", 1), ("bob@exemple.fr", 1), ("léa.müller@exemple.fr", 1), ("test@recette.local", 2)], 1):
    accounts[i] = dict(id=i, server_id=sid, email=e, login=e, has_password=True, status=["ok", "new", "auth_failed", "ok"][i - 1], last_test=None,
                       pst_id=None, options=dict(mail=True, calendar=True, contacts=True), job=None)
ID["account"] = 4

def publish(ev, obj):
    data = json.dumps(obj)
    for q in list(CLIENTS):
        q.put((ev, data))

def sleep_scan(p):
    time.sleep(2)
    p["status"] = "valid"
    p["scan"] = dict(folders=[dict(path=["Boîte de réception"], kind="mail", count=4210), dict(path=["Boîte de réception", "Clients é à ü"], kind="mail", count=812),
                              dict(path=["Éléments envoyés"], kind="mail", count=1530), dict(path=["Contacts"], kind="contacts", count=214), dict(path=["Calendrier"], kind="calendar", count=96)],
                     totals=dict(mail=6552, contacts=214, calendar=96), errors=[])
    publish("pst", p)

def acc_job(a):
    a["job"] = jobs.get(a.get("_job"))

def run_job(j):
    def say(l, m):
        logs[j["id"]].append(dict(n=len(logs[j["id"]]) + 1, ts=time.time(), level=l, msg=m))
    say("info", "Pré-vérifications : login IMAP OK, quota suffisant"); j.update(status="running", phase="precheck", started_at=time.time())
    publish("job", j); time.sleep(1.5)
    folders = {"mail": ["INBOX", "INBOX/Clients é à ü", "Sent"], "calendar": ["Calendrier"], "contacts": ["Contacts"]}
    for kind in ("mail", "calendar", "contacts"):
        j["phase"] = kind
        total = j["progress"][kind]["total"]
        while j["progress"][kind]["done"] < total:
            while j["status"] in ("paused",):
                time.sleep(0.3)
            if j["status"] == "stopping":
                j.update(status="stopped", finished_at=time.time()); say("warn", "Arrêt demandé, job arrêté"); publish("job", j); return
            step = random.randint(20, 60)
            p = j["progress"][kind]
            p["done"] = min(total, p["done"] + step)
            if random.random() < 0.05:
                p["errors"] += 1; logs[j["id"] + 100000].append(dict(kind=kind, folder="INBOX", item_key=f"{random.randint(1000,9999)}", error="APPEND refusé : message trop gros")); say("error", f"[{kind}] erreur sur un élément")
            j["current_folder"] = random.choice(folders[kind])
            j["rate"] = round(step / 0.4, 1); j["eta"] = int((total - p["done"]) / max(1, j["rate"]))
            say("info", f"[{kind}] {j['current_folder']} : {p['done']}/{total}")
            publish("job", j); time.sleep(0.4)
    j["phase"] = "verify"; j["current_folder"] = None; say("info", "Vérification serveur (STATUS, échantillon)"); publish("job", j); time.sleep(1)
    errs = sum(j["progress"][k]["errors"] for k in ("mail", "calendar", "contacts"))
    j.update(status="done_with_errors" if errs else "done", finished_at=time.time(), rate=0, eta=0)
    say("info", "Terminé"); publish("job", j)

def new_job(a):
    jid = nid("job")
    j = dict(id=jid, account_id=a["id"], status="queued", phase="precheck", current_folder=None, rate=0, eta=None, started_at=None, finished_at=None, error=None,
             progress=dict(mail=dict(done=0, total=900, errors=0), calendar=dict(done=0, total=120, errors=0), contacts=dict(done=0, total=80, errors=0)))
    jobs[jid] = j; logs[jid] = []; logs[jid + 100000] = []
    a["job"] = j
    publish("job", j)
    threading.Thread(target=run_job, args=(j,), daemon=True).start()
    return j

def report(j):
    mk = lambda k: dict(sent=j["progress"][k]["done"] - j["progress"][k]["errors"], present=j["progress"][k]["done"] - j["progress"][k]["errors"], errors=j["progress"][k]["errors"], warnings=2 if k == "mail" else 0, skipped=0)
    return dict(job_id=j["id"], mail=mk("mail"), calendar=mk("calendar"), contacts=mk("contacts"),
                folders=[dict(path=["INBOX"], sent=500, present=500), dict(path=["INBOX", "Clients é à ü"], sent=300, present=300)], sample_check="20/20 mails relus, hash identiques")

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send(self, code=200, body=None, ctype="application/json", headers=None):
        if body is None: body = b""
        elif not isinstance(body, bytes): body = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def authed(self):
        for c in self.headers.get("Cookie", "").split(";"):
            if c.strip().startswith("sid=") and c.strip()[4:] in SESSIONS: return True
        return False
    def body(self):
        if getattr(self, "_raw", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            self._raw = self.rfile.read(n) if n else b""
        return self._raw
    def js(self):
        b = self.body()
        try: return json.loads(b or b"{}")
        except Exception: return {}
    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")

    def route(self, m):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        self._raw = None; self.body()
        if not p.startswith("/api"):
            return self.static(p)
        p = p[4:] or "/"
        try:
            if p == "/health": return self.send(200, dict(ok=True))
            if p == "/login" and m == "POST":
                if self.js().get("password") == "admin":
                    sid = uuid.uuid4().hex; SESSIONS.add(sid)
                    return self.send(200, dict(ok=True), headers={"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
                return self.send(401, dict(detail="Mot de passe incorrect"))
            if not self.authed(): self.body(); return self.send(401, dict(detail="Non authentifié"))
            return self.api(m, p, q)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e:
            import traceback; traceback.print_exc(); self.send(500, dict(detail=str(e)))

    def api(self, m, p, q):
        parts = p.strip("/").split("/")
        if p == "/logout": return self.send(200, dict(ok=True), headers={"Set-Cookie": "sid=; Path=/; Max-Age=0"})
        if p == "/me": return self.send(200, dict(authenticated=True))
        if p == "/events":
            qu = queue.Queue(); CLIENTS.append(qu)
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "close"); self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b": hello\n\n"); self.wfile.flush()
                while True:
                    try: ev, data = qu.get(timeout=10); self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
                    except queue.Empty: self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception: pass
            finally: CLIENTS.remove(qu)
            return
        if parts[0] == "servers":
            if len(parts) == 1:
                if m == "GET": return self.send(200, list(servers.values()))
                s = self.js(); s["id"] = nid("server"); servers[s["id"]] = s; return self.send(201, s)
            sid = int(parts[1]); s = servers.get(sid)
            if not s: return self.send(404, dict(detail="Serveur introuvable"))
            if len(parts) == 3 and parts[2] == "test":
                return self.send(200, dict(imap=dict(ok=True, msg="Connexion et TLS OK"), caldav=dict(ok=s["dav_enabled"], msg="PROPFIND 207" if s["dav_enabled"] else "désactivé"), carddav=dict(ok=s["dav_enabled"], msg="PROPFIND 207" if s["dav_enabled"] else "désactivé")))
            if m == "GET": return self.send(200, s)
            if m == "PUT": s.update(self.js()); s["id"] = sid; return self.send(200, s)
            if m == "DELETE":
                servers.pop(sid); [accounts.pop(k) for k in [k for k, a in accounts.items() if a["server_id"] == sid]]; return self.send(200, dict(ok=True))
        if parts[0] == "accounts":
            if p == "/accounts/csv-template": return self.send(200, "email;password;login\nalice@exemple.fr;motdepasse;\n", "text/csv")
            if p == "/accounts/import-csv":
                raw = self.body().decode("utf-8", "replace"); lines = [l for l in raw.splitlines() if ";" in l or "@" in l]
                body = raw.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in raw else raw
                created = 0; errors = []
                rows = [l for l in body.split("\n") if l.strip() and not l.startswith("--")]
                for i, l in enumerate(rows, 1):
                    f = [x.strip() for x in l.replace(",", ";").split(";")]
                    if f[0].lower() in ("email", "mail"): continue
                    if "@" not in f[0] or len(f) < 2 or not f[1]: errors.append(dict(line=i, msg=f"Ligne invalide : « {l.strip()[:40]} »")); continue
                    aid = nid("account"); accounts[aid] = dict(id=aid, server_id=1, email=f[0], login=f[2] if len(f) > 2 and f[2] else f[0], has_password=True, status="new", last_test=None, pst_id=None, options=dict(mail=True, calendar=True, contacts=True), job=None); created += 1
                return self.send(200, dict(created=created, updated=0, errors=errors))
            if len(parts) == 1:
                if m == "GET":
                    s = q.get("server_id", [None])[0]; return self.send(200, [a for a in accounts.values() if not s or a["server_id"] == int(s)])
                b = self.js(); aid = nid("account")
                a = dict(id=aid, server_id=b["server_id"], email=b["email"], login=b.get("login") or b["email"], has_password=True, status="new", last_test=None, pst_id=None, options=dict(mail=True, calendar=True, contacts=True), job=None)
                accounts[aid] = a; return self.send(201, a)
            aid = int(parts[1]); a = accounts.get(aid)
            if not a: self.body(); return self.send(404, dict(detail="Compte introuvable"))
            if len(parts) == 3 and parts[2] == "test":
                time.sleep(0.4); ok = "müller" not in a["email"]
                a["status"] = "ok" if ok else "auth_failed"; a["last_test"] = time.time()
                r = dict(imap=dict(ok=ok, msg="OK" if ok else "AUTHENTICATIONFAILED", quota=dict(used_kb=120000, limit_kb=2000000) if ok else None),
                         caldav=dict(ok=ok and servers[a["server_id"]]["dav_enabled"], msg=""), carddav=dict(ok=ok and servers[a["server_id"]]["dav_enabled"], msg=""))
                publish("account", a); return self.send(200, r)
            if len(parts) == 3 and parts[2] == "pst":
                a["pst_id"] = self.js().get("pst_id"); publish("account", a); return self.send(200, a)
            if m == "PUT":
                b = self.js(); b.pop("password", None); a.update(b); a["id"] = aid; return self.send(200, a)
            if m == "DELETE": accounts.pop(aid); return self.send(200, dict(ok=True))
        if parts[0] == "uploads":
            if len(parts) == 1 and m == "POST":
                b = self.js(); key = (b["filename"], b["size"], b["last_modified"])
                u = next((x for x in uploads.values() if x["key"] == key), None)
                if not u:
                    uid = str(uuid.uuid4())[:8]; u = dict(id=uid, key=key, filename=b["filename"], size=b["size"], chunk_size=CHUNK, received={}, status="uploading", n=(b["size"] + CHUNK - 1) // CHUNK)
                    uploads[uid] = u
                return self.send(200, dict(id=u["id"], chunk_size=CHUNK, received=sorted(u["received"])))
            u = uploads.get(parts[1])
            if not u: self.body(); return self.send(404, dict(detail="Upload inconnu"))
            if len(parts) == 1 + 1 and m == "GET": return self.send(200, dict(id=u["id"], filename=u["filename"], size=u["size"], chunk_size=CHUNK, received=sorted(u["received"]), status=u["status"]))
            if len(parts) == 4 and parts[2] == "chunks" and m == "PUT":
                global chunk_counter
                data = self.body(); n = int(parts[3])
                time.sleep(DELAY)
                with LOCK: chunk_counter += 1; c = chunk_counter
                if FAIL_EVERY and c % FAIL_EVERY == 0: return self.send(503, dict(detail="Service indisponible (simulé)"))
                if hashlib.sha256(data).hexdigest() != self.headers.get("X-Chunk-Sha256"): return self.send(422, dict(detail="Hash de chunk invalide"))
                exp = min(CHUNK, u["size"] - n * CHUNK)
                if n >= u["n"] or len(data) != exp: return self.send(400, dict(detail="Taille de chunk inattendue"))
                u["received"][n] = len(data); return self.send(200, dict(ok=True, n=n))
            if len(parts) == 3 and parts[2] == "complete":
                if len(u["received"]) != u["n"]: return self.send(409, dict(detail="Chunks manquants"))
                pid = nid("pst"); pst = dict(id=pid, filename=u["filename"], size=u["size"], status="validating", scan=None, error=None); psts[pid] = pst
                u["status"] = "complete"; publish("pst", pst); threading.Thread(target=sleep_scan, args=(pst,), daemon=True).start()
                return self.send(200, dict(pst_id=pid, id=pid, status="validating"))
        if parts[0] == "psts":
            if len(parts) == 1: return self.send(200, list(psts.values()))
            pst = psts.get(int(parts[1]))
            if not pst: return self.send(404, dict(detail="PST introuvable"))
            if m == "DELETE": psts.pop(pst["id"]); return self.send(200, dict(ok=True))
            return self.send(200, pst)
        if p == "/runs" and m == "POST":
            return self.send(200, [new_job(accounts[i]) for i in self.js().get("account_ids", []) if i in accounts])
        if parts[0] == "jobs":
            if len(parts) == 1: return self.send(200, [j for j in jobs.values() if j["status"] in ("queued", "running", "paused", "stopping")])
            j = jobs.get(int(parts[1]))
            if not j: return self.send(404, dict(detail="Job introuvable"))
            if len(parts) == 2: return self.send(200, j)
            act = parts[2]
            if act == "pause": j["status"] = "paused"; publish("job", j); return self.send(200, j)
            if act == "resume": j["status"] = "running"; publish("job", j); return self.send(200, j)
            if act == "stop": j["status"] = "stopping"; publish("job", j); return self.send(200, j)
            if act in ("retry", "retry-errors"):
                self.body(); a = accounts[j["account_id"]]; return self.send(200, new_job(a))
            if act == "log":
                after = int(q.get("after", [0])[0] or 0); L = logs[j["id"]][after:]
                return self.send(200, dict(lines=L, next=after + len(L)))
            if act == "errors": return self.send(200, logs.get(j["id"] + 100000, []))
            if act == "report":
                if j["status"] in ("queued", "running", "paused"): return self.send(404, dict(detail="Rapport non disponible"))
                return self.send(200, report(j))
        self.send(404, dict(detail="Not found: " + p))

    def static(self, p):
        f = (ROOT / p.lstrip("/")).resolve()
        if p == "/" or not f.is_file() or ROOT not in f.parents:
            f = ROOT / "index.html"
        ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self.send(200, f.read_bytes(), ct, {"Cache-Control": "no-store"})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 22362
    ThreadingHTTPServer.daemon_threads = True
    print(f"Mock sur http://0.0.0.0:{port} (mot de passe : admin)")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
