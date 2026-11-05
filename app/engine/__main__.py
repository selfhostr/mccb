"""python -m app.engine dump <pst> <outdir> [--limit N]  |  scan <pst>"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from . import PstFile, verify_message


def _safe(s: str) -> str:
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", s).strip() or "_"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="write .eml/.ics/.vcf for inspection")
    d.add_argument("pst")
    d.add_argument("outdir")
    d.add_argument("--limit", type=int, default=0, help="max items per folder (0 = all)")
    s = sub.add_parser("scan", help="print the scan JSON")
    s.add_argument("pst")
    a = ap.parse_args(argv)
    if a.cmd == "scan":
        with PstFile(a.pst) as p:
            print(json.dumps(p.scan(), ensure_ascii=False, indent=1))
        return 0
    sha = hashlib.sha256(os.path.abspath(a.pst).encode()).hexdigest()[:16]
    stats = {"mail": 0, "calendar": 0, "contacts": 0, "errors": 0, "warnings": 0}
    with PstFile(a.pst) as p:
        for f in p.folders():
            if f.kind not in ("mail", "calendar", "contacts"):
                continue
            fdir = os.path.join(a.outdir, *[_safe(x) for x in f.path])
            os.makedirs(fdir, exist_ok=True)
            for i, key, h in p.iter_items(f):
                if a.limit and i >= a.limit:
                    break
                try:
                    if f.kind == "mail":
                        b = p.build_message(h, "%s-%s" % (sha, key))
                        if b is None:
                            continue
                        errs = verify_message(b, h)
                        with open(os.path.join(fdir, "%04d.eml" % i), "wb") as fh:
                            fh.write(b.raw)
                        stats["mail"] += 1
                        if b.warnings or errs:
                            stats["warnings"] += 1
                            print("%s #%d %s warnings=%s errors=%s" % ("/".join(f.path), i, b.subject[:40],
                                                                     b.warnings, errs))
                    else:
                        o = p.build_event(h) if f.kind == "calendar" else p.build_contact(h)
                        if o is None:
                            continue
                        with open(os.path.join(fdir, o.filename), "w", encoding="utf-8", newline="") as fh:
                            fh.write(o.content)
                        stats[f.kind] += 1
                        if o.warnings:
                            print("%s #%d %s warnings=%s" % ("/".join(f.path), i, o.summary[:40], o.warnings))
                except Exception as ex:  # noqa: BLE001
                    stats["errors"] += 1
                    print("ERROR %s #%d: %s" % ("/".join(f.path), i, ex), file=sys.stderr)
        scan = p.scan()
    print(json.dumps({"written": stats, "scan_errors": scan["errors"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
