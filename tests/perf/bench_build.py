"""Build + verify benchmark (producer side of a mail import).

    python tests/perf/bench_build.py                 # fixtures + synthetic heavy items, in-process
    python tests/perf/bench_build.py --profile       # + cProfile top hotspots
    python tests/perf/bench_build.py --procs 1,4,8   # + parallel builders (app.server.builders) on the fixtures

Measures, per item, what the job does before the APPEND: PstFile.build_message + verify_message + hash.
Not collected by pytest (no test_ prefix); needs pypff (run it in the test image).
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import os
import pstats
import random
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.engine import PstFile, verify_message  # noqa: E402
from app.engine import mapi  # noqa: E402
from app.engine.message import MessageBuilder  # noqa: E402
from app.engine.verify import check_raw  # noqa: E402
from tests.engine.conftest import BIN, BOOL, I32, U, FakeAtt, FakeMsg, fixture_paths  # noqa: E402

MB = 1_000_000


def norm_hash(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b"\r\n", b"\n").rstrip(b"\n")).hexdigest()


# ------------------------------------------------------------------ fixtures
def bench_fixtures(limit: int | None = None) -> tuple[int, int, float]:
    n = nbytes = 0
    t0 = time.perf_counter()
    for path in fixture_paths():
        try:
            pst = PstFile(path)
        except Exception:  # noqa: BLE001 - corrupt fixture
            continue
        with pst:
            for f in pst.folders():
                if f.kind != "mail":
                    continue
                for idx, key, h in pst.iter_items(f):
                    try:
                        b = pst.build_message(h, "<bench-%s>" % key)
                    except Exception:  # noqa: BLE001
                        continue
                    if b is None:
                        continue
                    verify_message(b, h)
                    norm_hash(b.raw)
                    n += 1
                    nbytes += len(b.raw)
                    if limit and n >= limit:
                        return n, nbytes, time.perf_counter() - t0
    return n, nbytes, time.perf_counter() - t0


# ------------------------------------------------------------------ synthetic heavy items
_WORDS = ("Réunion budget été déjà prévu – « citation » naïve façon Größe Straße "
          "Привет мир как дела 你好世界 こんにちは世界 안녕하세요 مرحبا بالعالم שלום עולם "
          "Γειά σου κόσμε Merhaba dünya ąęśćżź Ελλάδα").split()


def _text(n_chars: int, seed: int) -> str:
    r = random.Random(seed)
    out, size = [], 0
    while size < n_chars:
        w = r.choice(_WORDS)
        out.append(w)
        size += len(w) + 1
        if r.random() < 0.08:
            out.append("\n")
    return " ".join(out)


def _html(n_bytes: int, seed: int) -> bytes:
    r = random.Random(seed)
    rows, size = [], 0
    while size < n_bytes:
        row = ('<tr><td style="font-family:Calibri;color:#1f497d">%s</td><td><a href="https://ex.test/%d">l</a>'
               '</td></tr>\n' % (" ".join(r.choice(_WORDS) for _ in range(12)), r.randrange(10**6)))
        rows.append(row)
        size += len(row.encode())
    body = ('<html><head><meta charset="utf-8"><style>p{margin:0}</style></head><body><table>%s</table>'
            '<img src="cid:image001.png@01D9AB12.34567890"><img src="cid:filelist.xml@01D9AB12.34567890">'
            '</body></html>' % "".join(rows))
    return body.encode("utf-8")


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _base_props(subject: str) -> dict:
    return {mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_SUBJECT: U(subject),
            mapi.PR_SENDER_NAME: U("Jérôme"), mapi.PR_SENDER_EMAIL: U("j@example.fr"),
            mapi.PR_SENDER_ADDRTYPE: U("SMTP")}


def synthetic_items() -> list[tuple[str, FakeMsg]]:
    items = []
    for mb in (2, 5):
        p = _base_props("HTML %d MB" % mb)
        p[mapi.PR_HTML] = BIN(_html(mb * MB, mb))
        p[mapi.PR_INTERNET_CPID] = I32(65001)
        img = FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U("image001.png"),
                       mapi.PR_ATTACH_CONTENT_ID: U("image001.png@01D9AB12.34567890"),
                       mapi.PR_ATTACHMENT_HIDDEN: BOOL(True)}, PNG)
        items.append(("html-%dMB" % mb, FakeMsg(p, attachments=[img])))
    r = random.Random(7)
    atts = []
    for i in range(20):
        data = r.randbytes(5 * MB)
        name = ("rapport-%02d.pdf" % i) if i % 2 else ("données-%02d.bin" % i)
        if i % 2:
            data = b"%PDF-1.7\n" + data
        atts.append(FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U(name)}, data))
    p = _base_props("20 x 5 MB attachments")
    p[mapi.PR_BODY] = U("Voir pièces jointes.")
    items.append(("att-20x5MB", FakeMsg(p, attachments=atts)))
    p = _base_props("Long multilingual text")
    p[mapi.PR_BODY] = U(_text(3 * MB, 3))
    items.append(("text-3Mchars", FakeMsg(p)))
    return items


def bench_synthetic(repeat: int = 1) -> list[tuple[str, float, int]]:
    out = []
    for name, src in synthetic_items():
        best = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            built = MessageBuilder("<bench-%s>" % name).build_raw(src)
            errs = check_raw(built.raw, built.expect)  # = verify_message(built) on a BuiltMessage
            norm_hash(built.raw)
            dt = time.perf_counter() - t0
            assert not errs, (name, errs)
            assert "fallback" not in built.warnings, (name, built.warnings)
            best = dt if best is None else min(best, dt)
        out.append((name, best, len(built.raw)))
    return out


# ------------------------------------------------------------------ parallel builders
def bench_procs(n: int, repeat: int) -> tuple[int, int, float]:
    """Steady state of n builder processes: one warm pool per PST (start-up excluded), every mail item
    of the PST built `repeat` times."""
    from app.server.builders import BuildTask, BuilderPool
    n_ok = nbytes = 0
    total = 0.0
    for path in fixture_paths():
        try:
            pst = PstFile(path)
        except Exception:  # noqa: BLE001
            continue
        with pst:
            mine = [BuildTask(fi, list(f.path), idx, key, "<bench-%s>" % key)
                    for fi, f in enumerate(pst.folders()) if f.kind == "mail"
                    for idx, key, h in pst.iter_items(f)]
        if not mine:
            continue
        pool = BuilderPool(path, n, in_child=False)
        try:
            while not all(s.ready for s in pool.slots):  # warm-up: processes started, PST opened
                pool.wait(0.5)
            pending = mine * repeat
            t0 = time.perf_counter()
            while pending or pool.busy():
                while pending and pool.can_dispatch():
                    pool.dispatch(pending.pop())
                for ev in pool.wait(0.5):
                    if ev.result is not None and ev.result.get("raw"):
                        n_ok += 1
                        nbytes += len(ev.result["raw"])
            total += time.perf_counter() - t0
        finally:
            pool.close()
    return n_ok, nbytes, total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--procs", default="", help="comma-separated builder counts, e.g. 1,4,8")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()

    n, nb, dt = bench_fixtures()
    print("fixtures (in-process): %d items, %.1f MB in %.2f s -> %.1f items/s, %.2f MB/s"
          % (n, nb / MB, dt, n / dt, nb / MB / dt))
    for name, dt, size in bench_synthetic():
        print("synthetic %-14s %7.2f MB  %7.3f s  -> %6.2f MB/s" % (name, size / MB, dt, size / MB / dt))
    for p in [int(x) for x in a.procs.split(",") if x.strip()]:
        n, nb, dt = bench_procs(p, a.repeat)
        print("fixtures x%d, %d builder process(es) (steady state): %d items, %.1f MB in %.2f s -> %.1f items/s, %.2f MB/s"
              % (a.repeat, p, n, nb / MB, dt, n / dt, nb / MB / dt))
    if a.profile:
        for label, fn in (("fixtures", bench_fixtures), ("synthetic", bench_synthetic)):
            pr = cProfile.Profile()
            pr.enable()
            fn()
            pr.disable()
            s = io.StringIO()
            pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18)
            print("\n==== cProfile (%s, by tottime) ====" % label)
            print("\n".join(s.getvalue().splitlines()[:45]))


if __name__ == "__main__":
    main()
