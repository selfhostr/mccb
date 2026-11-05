"""Isolated execution subprocess: one job or one PST analysis per process.

libpff (native code) may crash (SIGSEGV) on a corrupted PST: the parent worker survives, notices the death of the
subprocess and applies recovery (faulty item marked "libpff crash", see worker.py).

    python -m app.server.child job <job_id> <worker_id>
    python -m app.server.child validate <pst_id>

Test hook PSTMIG_TEST_CRASH: see crash.maybe_crash (active only in this subprocess).
"""
from __future__ import annotations

import logging
import signal
import sys
import time


def _terminated(*_):
    raise SystemExit(143)


def main(argv: list[str]) -> int:
    from . import crash
    crash.IN_CHILD = True
    crash.die_with_parent()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _terminated)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C: handled by the parent
    from . import db
    if len(argv) >= 3 and argv[0] == "job":
        job_id, worker_id = int(argv[1]), argv[2]
        from .runner import run_job
        try:
            run_job(job_id, worker_id)
        except Exception as e:  # last safety net: the job never stays "running"
            logging.getLogger("child").exception("job %s", job_id)
            db.ex("UPDATE jobs SET status='failed', error=?, worker_id=NULL, finished_at=? WHERE id=?",
                  (f"{type(e).__name__}: {e}", time.time(), job_id))
        return 0
    if len(argv) >= 2 and argv[0] == "validate":
        pid = int(argv[1])
        from .uploads import validate
        try:
            validate(pid)
        except Exception as e:
            logging.getLogger("child").exception("validation %s", pid)
            db.ex("UPDATE psts SET status='invalid', error=? WHERE id=? AND status='validating'", (str(e), pid))
        return 0
    print("usage: python -m app.server.child job <id> <worker_id> | validate <pst_id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
