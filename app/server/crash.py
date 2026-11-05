"""Isolation des crashs natifs (libpff) : marqueur « élément en cours », compteur de crashs, crochet de test.

Le sous-processus (child.py) enregistre dans `kv` l'élément qu'il s'apprête à lire avec libpff
(`job:<id>:current`). Si le process meurt par un signal, le worker parent lit ce marqueur : l'élément fautif
est marqué `error` « crash libpff » (jamais un élément `inflight`, résolu à la reprise par UID SEARCH), le job est
remis en file et reprend après lui. Au-delà de MAX_CHILD_CRASHES crashs du même job, il passe en `failed`.
"""
from __future__ import annotations

import logging
import os
import signal
import time

from . import db, repo
from .config import settings

CRASH_MSG = "crash libpff"
VALIDATE_CRASH_MSG = "crash libpff pendant l'analyse"
TEST_CRASH_ENV = "PSTMIG_TEST_CRASH"
TEST_DELAY_ENV = "PSTMIG_TEST_ITEM_DELAY"  # secondes de pause avant chaque élément (tests e2e : pause/arrêt/kill)
PARENT_PID_ENV = "PSTMIG_PARENT_PID"
IN_CHILD = False  # positionné par child.main()

log = logging.getLogger("crash")


# ------------------------------------------------------------------ côté sous-processus
def maybe_crash(*tokens: str) -> None:
    """Crochet de test : PSTMIG_TEST_CRASH = liste séparée par des virgules parmi
    « validate » (analyse PST), « open » (ouverture du PST), « * » (chaque élément), « <item_key> »,
    « fetch:<index> » (lecture de l'élément n° index d'un dossier). Envoie SIGSEGV au sous-processus.
    Sans effet hors sous-processus ou si la variable est absente."""
    if not IN_CHILD:
        return
    spec = os.environ.get(TEST_CRASH_ENV, "")
    if not spec:
        return
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    if any(t in wanted for t in tokens if t):
        log.warning("%s : SIGSEGV simulé (%s)", TEST_CRASH_ENV, tokens)
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)  # pragma: no cover


def maybe_delay() -> None:
    """Crochet de test : PSTMIG_TEST_ITEM_DELAY = délai (s) avant la lecture de chaque élément, pour laisser aux
    tests de bout en bout le temps de mettre en pause / arrêter / tuer le worker pendant un import réel.
    Sans effet hors sous-processus ou si la variable est absente."""
    if not IN_CHILD:
        return
    try:
        d = float(os.environ.get(TEST_DELAY_ENV, "") or 0)
    except ValueError:
        return
    if d > 0:
        time.sleep(min(d, 10.0))


def die_with_parent() -> None:
    """Linux : le sous-processus reçoit SIGKILL si le worker parent meurt (pas d'orphelin qui continue)."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # pragma: no cover - non Linux
        return
    ppid = os.environ.get(PARENT_PID_ENV)
    if ppid and ppid.isdigit() and os.getppid() != int(ppid):
        os._exit(1)  # parent déjà mort avant prctl


def _mkey(job_id: int) -> str:
    return f"job:{job_id}:current"


def set_marker(job_id: int, marker: dict | None) -> None:
    if marker is None:
        db.ex("DELETE FROM kv WHERE key=?", (_mkey(job_id),))
    else:
        db.kv_set(_mkey(job_id), db.jdump(marker))


def get_marker(job_id: int) -> dict | None:
    return db.jload(db.kv_get(_mkey(job_id)), None)


def get_crashes(job_id: int) -> int:
    try:
        return int(db.kv_get(f"job:{job_id}:crashes") or 0)
    except ValueError:
        return 0


def reset_crashes(job_id: int) -> None:
    db.ex("DELETE FROM kv WHERE key IN (?, ?)", (f"job:{job_id}:crashes", _mkey(job_id)))


def get_skips(pst_key: str) -> set[tuple[str, str, int]]:
    """Index d'éléments dont la simple lecture (iter_items) fait planter libpff : sautés à l'itération.
    Rattachés à l'empreinte du PST (`pst_key`, comme la table items), pas à son id."""
    return {(k, f, int(i)) for k, f, i in db.jload(db.kv_get(f"pst:{pst_key}:crash_skips"), [])}


def _add_skip(pst_key: str, kind: str, folder: str, idx: int) -> None:
    s = get_skips(pst_key)
    s.add((kind, folder, int(idx)))
    db.kv_set(f"pst:{pst_key}:crash_skips", db.jdump(sorted(s)))


def describe_rc(rc: int | None) -> str:
    if rc is None:
        return "fin inconnue"
    if rc < 0:
        try:
            return f"signal {signal.Signals(-rc).name}"
        except ValueError:
            return f"signal {-rc}"
    return f"code de sortie {rc}"


# ------------------------------------------------------------------ côté worker parent
def _blame(marker: dict, job_id: int, why: str) -> str | None:
    """Marque l'élément désigné par le marqueur en erreur « crash libpff ». Retourne sa description."""
    kind, folder, aid, pid = marker.get("kind"), marker.get("folder") or "", marker.get("a"), marker.get("p")
    if not (kind and aid and pid):
        return None
    pkey = marker.get("s") or db.pst_key_of(db.q1("SELECT id, pst_sha FROM psts WHERE id=?", (pid,))) or f"id:{pid}"
    key = marker.get("key")
    now = time.time()
    if key:
        desc = f"élément {key} (dossier « {folder} », n° {marker.get('idx')})"
    else:
        # crash pendant la lecture de l'élément n° idx : pas de clé connue → index sauté à l'itération
        key = f"{folder}#crash{marker.get('idx')}"
        _add_skip(pkey, kind, folder, int(marker.get("idx") or 0))
        desc = f"lecture de l'élément n° {marker.get('idx')} (dossier « {folder} »)"
    err = f"{CRASH_MSG} ({why}) — élément ignoré"
    # jamais un élément déjà `ok` ou `inflight` (peut-être APPENDé : vérifié à la reprise par _resolve_inflight)
    db.ex("INSERT INTO items(account_id,pst_id,pst_key,kind,item_key,folder,status,error,job_id,updated_at) "
          "VALUES (?,?,?,?,?,?,'error',?,?,?) "
          "ON CONFLICT(account_id,pst_key,kind,item_key) DO UPDATE SET status='error', error=excluded.error, "
          "job_id=excluded.job_id, updated_at=excluded.updated_at WHERE items.status NOT IN ('ok','inflight')",
          (aid, pid, pkey, kind, key, folder, err, job_id, now))
    return desc


def handle_job_crash(job_id: int, rc: int | None) -> str:
    """Le sous-processus du job est mort anormalement. Retourne le nouveau statut du job."""
    why = describe_rc(rc)
    job = db.q1("SELECT status FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return "deleted"
    st = job["status"]
    if st not in ("running", "stopping", "paused"):
        # job déjà terminé (crash à la fermeture du process) : rien à reprendre
        set_marker(job_id, None)
        repo.log(job_id, f"Sous-processus terminé anormalement ({why}) après la fin du job", "warning")
        return st
    marker = get_marker(job_id) or {}
    desc = _blame(marker, job_id, why)
    set_marker(job_id, None)
    n = get_crashes(job_id) + 1
    db.kv_set(f"job:{job_id}:crashes", str(n))
    where = desc or f"hors élément (étape {marker.get('phase') or 'inconnue'})"
    repo.log(job_id, f"Crash du sous-processus ({why}, {n}/{settings.max_child_crashes}) : {where}"
             + (f" — marqué en erreur « {CRASH_MSG} », reprise après cet élément" if desc else ""), "error")
    now = time.time()
    if st == "stopping":
        db.ex("UPDATE jobs SET status='stopped', worker_id=NULL, finished_at=?, rate=NULL, eta=NULL, "
              "current_folder=NULL WHERE id=? AND status='stopping'", (now, job_id))
        reset_crashes(job_id)
        return "stopped"
    if st == "paused":
        db.ex("UPDATE jobs SET worker_id=NULL, rate=NULL, eta=NULL WHERE id=?", (job_id,))
        return "paused"
    if n >= settings.max_child_crashes:
        msg = f"Le sous-processus a planté {n} fois (crash libpff probable)"
        db.ex("UPDATE jobs SET status='failed', error=?, worker_id=NULL, finished_at=?, rate=NULL, eta=NULL, "
              "current_folder=NULL WHERE id=? AND status='running'", (msg, now, job_id))
        repo.log(job_id, f"Échec : {msg}", "error")
        reset_crashes(job_id)
        return "failed"
    db.ex("UPDATE jobs SET status='queued', worker_id=NULL, rate=NULL, eta=NULL WHERE id=? AND status='running'",
          (job_id,))
    return "queued"


def handle_validate_crash(pst_id: int, rc: int | None) -> None:
    db.ex("UPDATE psts SET status='invalid', error=?, updated_at=? WHERE id=? AND status='validating'",
          (f"{VALIDATE_CRASH_MSG} ({describe_rc(rc)}) : fichier PST probablement corrompu", time.time(), pst_id))
