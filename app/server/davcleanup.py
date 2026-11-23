"""CalDAV / CardDAV collections an earlier version of mccb left on the target, deleted at the end of a run (docs/dav.md).

Two kinds of them: the second "Contacts" (calendar, task list) an earlier version created next to the server's own,
whose items ConnectorJobRunner._relocate copied into the collection their folder now goes to; and the copies of the
collections another account shares with the source mailbox, or the public ones of its domain, which are no longer
migrated with it (unless DAV_INCLUDE_SHARED=1).

A collection is deleted only when all of this holds:
- mccb created it (its URL: dav.made_by_mccb), and no folder of this run goes to it;
- no item of any migration is recorded in it any more (status ok, inflight, pending or error);
- every resource in it, as the server lists it now, is a copy mccb made for this mailbox that is no longer needed
  there: an item recorded as moved out of it (_relocate, or the `dav_moved` of an earlier report) and listed now in the
  collection its folder goes to, or an item of a shared folder this run leaves out.
Anything else keeps it, with a line saying why (an info line when nothing of this mailbox is in it). Only in a full
run or a final resync (delta): never in a mirror pass, never on a rerun of the errors. The source is never touched. A
copy of every object is kept under mirror-trash/ before anything is deleted.

One step is more than a deletion: SmarterMail keeps one object per UID in the whole account, so the copy left in the
old collection is what keeps the item out of the new one (its error names that collection). Such an item is moved:
deleted from the old collection, written into the new one, and written back where it was when the new one still
refuses it (the old collection is then kept)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import unquote

from ..connectors.base import ConnectorError, OutgoingItem
from ..connectors.dav import made_by_mccb, url_key
from . import db
from . import mirror as mm
from .config import settings

if TYPE_CHECKING:
    from .connector_runner import ConnectorJobRunner

MODES = ("full", "delta")  # the runs that delete: never the mirror, never a rerun of the errors only
LIVE = ("ok", "inflight", "pending", "error")  # a row in one of these says its item is (or will be) in its target
KINDS = {"carddav": ("contact",), "caldav": ("event", "task")}  # items.kind of the objects of each DAV kind


def moved_key(account_id: int, pst_key: str) -> str:
    return f"dav-moved:{account_id}:{pst_key}"


def record_moved(account_id: int, pst_key: str, old: str, name: str | None, folders: set[str]) -> None:
    """Keeps that the items of these folders were in `old` before _relocate sent them elsewhere: the proof the
    clean-up needs, the rows saying only where they are now."""
    key = moved_key(account_id, pst_key)
    d = db.jload(db.kv_get(key), {}) or {}
    if not isinstance(d, dict):
        d = {}
    e = d.setdefault(url_key(old), {})
    e["folders"] = sorted(set(e.get("folders") or []) | set(folders))
    if name:
        e["name"] = name
    db.kv_set(key, db.jdump(d))


def moved_from(account_id: int, pst_key: str) -> dict[str, set[str]]:
    """{url_key of a collection: folder keys whose items were moved out of it}: what record_moved kept, and the
    `dav_moved` of the earlier reports of the mailbox (the runs of a version that did not keep it)."""
    out: dict[str, set[str]] = {}
    d = db.jload(db.kv_get(moved_key(account_id, pst_key)), {}) or {}
    for u, e in (d.items() if isinstance(d, dict) else ()):
        out.setdefault(url_key(u), set()).update((e or {}).get("folders") or [])
    # this job's own file too: it is the report of its previous execution (a rerun keeps the job), this one's comes
    # at its end
    for r in db.q("SELECT id, report_path FROM jobs WHERE account_id=? ORDER BY id DESC LIMIT 200", (account_id,)):
        path = Path(r["report_path"]) if r["report_path"] else settings.reports_dir / f"job-{r['id']}.json"
        try:
            text = path.read_text(encoding="utf-8")
            if '"dav_moved"' not in text:
                continue
            rep = json.loads(text)
        except (OSError, ValueError):
            continue
        if not isinstance(rep, dict) or (rep.get("source") or {}).get("namespace") != pst_key:
            continue
        for m in rep.get("dav_moved") or []:
            if isinstance(m, dict) and m.get("from") and m.get("folder"):
                out.setdefault(url_key(m["from"]), set()).add(m["folder"])
    return out


# ------------------------------------------------------------------ the decision
@dataclass
class Verdict:
    delete: bool
    why: str = ""                                   # kept: why
    reasons: list[str] = field(default_factory=list)  # deleted: "moved", "shared"
    moves: list[tuple[str, dict, str]] = field(default_factory=list)  # (resource, its row, collection it goes to)
    quiet: bool = False                             # kept, nothing of this mailbox in it: an info line, no warning


def judge(url: str, resources: dict[str, str], *, rows_by_name: dict[str, list[dict]], rows_in: list[dict],
          live_other: int, evidence: set[str], plan_of: dict[tuple[str, str], str], shared: set[tuple[str, str]],
          include_shared: bool, present: Callable[[str], set[str] | None]) -> Verdict:
    """Whether the collection `url` an earlier run left goes. `resources`: {name: etag} of what it holds now;
    `rows_by_name`: the rows of this mailbox by resource name (casefold), each {id, kind, fk (its folder key now),
    status, target, error}; `rows_in`: those whose target is `url`; `live_other`: items of other migrations recorded in
    it; `evidence`: folder keys whose items were moved out of it; `plan_of`: (kind, folder key) -> the collection the
    folder goes to in this run; `shared`: (kind, folder key) of the shared folders this run leaves out; `present(T)`:
    the names (casefold) collection T lists now, None when it cannot be listed."""
    here = url_key(url)
    if not made_by_mccb(url):
        return Verdict(False, "it was not created by mccb", quiet=True)
    if live_other:
        return Verdict(False, f"{live_other} item(s) of another migration go to it", quiet=True)
    mine = [r for r in rows_in if r["status"] in LIVE]
    if mine:
        return Verdict(False, f"{len(mine)} item(s) this mailbox migrated are still recorded in it (gone from the "
                              "source since, or in a folder no longer migrated): deleting it would lose them")

    def is_shared(r: dict) -> bool:
        return (not include_shared and r["status"] == "skipped" and url_key(r["target"] or "") == here
                and (r["kind"], r["fk"]) in shared)

    def goes_to(r: dict) -> str | None:
        t = plan_of.get((r["kind"], r["fk"]))
        return t if t and url_key(t) != here and url_key(r["target"] or "") == url_key(t) else None

    any_shared = any(is_shared(r) for r in rows_in)
    if not resources:
        if not evidence and not any_shared:
            return Verdict(False, "nothing mccb wrote for this mailbox was ever in it", quiet=True)
        return Verdict(True, reasons=(["moved"] if evidence else []) + (["shared"] if any_shared else []))
    reasons: set[str] = set()
    moves: list[tuple[str, dict, str]] = []
    unknown: list[str] = []
    matched = 0
    for name in sorted(resources):
        rows = rows_by_name.get(name.casefold(), [])
        matched += bool(rows)
        if any(is_shared(r) for r in rows):
            reasons.add("shared")
            continue
        why, done = None, False
        for r in rows:
            t = goes_to(r)
            if t is None:
                continue
            if r["fk"] not in evidence:
                why = why or f"no record that '{name}' was moved out of it"
                continue
            there = present(t)
            if there is None:
                why = f"{t} could not be listed to find '{name}' there"
            elif name.casefold() in there and r["status"] == "ok":
                done = True
                break
            elif name.casefold() not in there and r["status"] == "error" and here in unquote(r["error"] or ""):
                moves.append((name, r, t))  # refused there because of its copy here (one object per UID)
                done = True
                break
            else:
                why = f"'{name}' is not in {t} yet"
        if done:
            reasons.add("moved")
        else:
            unknown.append(why or f"'{name}' was not written there by mccb for this mailbox")
    if unknown:
        if not matched and not evidence and not any_shared:
            return Verdict(False, "nothing in it was written by mccb for this mailbox", quiet=True)
        shown = "; ".join(unknown[:5]) + (f"; and {len(unknown) - 5} more" if len(unknown) > 5 else "")
        return Verdict(False, f"{len(unknown)} of its {len(resources)} item(s) are not copies known to be needed "
                              f"no more: {shown}")
    return Verdict(True, reasons=sorted(reasons), moves=moves)


# ------------------------------------------------------------------ the run
WHAT = {"moved": "copies an earlier run made, each of them now in the collection its folder goes to",
        "shared": "copies of a collection shared by another mailbox or public, no longer migrated with this one"}


class Cleanup:
    def __init__(self, runner: "ConnectorJobRunner", phases: list[str]):
        self.r = runner
        self.phases = [p for p in phases if hasattr(runner.targets.get(p), "listed")]  # the DAV ones
        self.sessions: dict = {}
        self._present: dict[str, set[str] | None] = {}
        self._evidence: dict[str, set[str]] | None = None
        self.touched: set[tuple[str, str]] = set()  # (phase, collection) an item was moved into

    def run(self) -> None:
        r = self.r
        if r.mode not in MODES or not self.phases:
            return
        try:
            for p in self.phases:
                self.sessions[p] = r.targets[p].session()
            by_kind: dict[str, list[str]] = {}
            for p in self.phases:
                by_kind.setdefault(self.sessions[p].dav_kind, []).append(p)
            planned = {url_key(fp.name) for plan in r.plans.values() for fp in plan.values()}
            for dav_kind, ps in by_kind.items():
                self._kind(dav_kind, ps, planned)
            self._recheck()
        finally:
            for s in self.sessions.values():
                s.close()

    # ---- one DAV kind (CalDAV: calendars and task lists share the home)
    def _kind(self, dav_kind: str, ps: list[str], planned: set[str]) -> None:
        r = self.r
        s = self.sessions[ps[0]]
        try:
            colls = r._on_session(s, s.collections_now)
        except ConnectorError as e:
            r.log(f"Collections earlier runs left on the target ({dav_kind}) not looked for: {e}", "warning")
            return
        cands = [(u, label) for u, label in colls if made_by_mccb(u) and url_key(u) not in planned]
        if not cands:
            return
        from .connector_runner import CARRIED
        from .runner import folder_key, legacy_folder_key
        kinds = KINDS[dav_kind]
        canon: dict[tuple[str, str], str] = {}
        plan_of: dict[tuple[str, str], str] = {}
        for f in r.folders:
            if f.kind in ps:
                fk = folder_key(f.path)
                for k in (fk, legacy_folder_key(f.path)):
                    canon[(CARRIED[f.kind], k)] = fk
                    if fk in r.plans.get(f.kind, {}):
                        plan_of[(CARRIED[f.kind], fk)] = r.plans[f.kind][fk].name
        shared: set[tuple[str, str]] = set()
        for kind, fk, legacy in r.shared_skipped:
            if kind in kinds:
                shared.add((kind, fk))
                canon[(kind, legacy)] = canon[(kind, fk)] = fk
        ph = ",".join("?" * len(kinds))
        rows = [dict(x) for x in db.q(
            f"SELECT id, kind, folder, status, target, hash, error, subject FROM items WHERE account_id=? AND "
            f"pst_key=? AND kind IN ({ph})", (r.account_id, r.pst_key, *kinds))]
        by_name: dict[str, list[dict]] = {}
        by_target: dict[str, list[dict]] = {}
        for x in rows:
            x["fk"] = canon.get((x["kind"], x["folder"]), x["folder"])
            if x["hash"]:
                by_name.setdefault(x["hash"].casefold(), []).append(x)
            if x["target"]:
                by_target.setdefault(url_key(x["target"]), []).append(x)
        live: dict[str, int] = {}  # every migration, this one too
        for x in db.q(f"SELECT target, COUNT(*) n FROM items WHERE kind IN ({ph}) AND status IN "
                      f"({','.join('?' * len(LIVE))}) AND target IS NOT NULL GROUP BY target", (*kinds, *LIVE)):
            live[url_key(x["target"])] = live.get(url_key(x["target"]), 0) + x["n"]
        if self._evidence is None:
            self._evidence = moved_from(r.account_id, r.pst_key)
        for url, label in cands:
            r.check()
            here = url_key(url)
            rows_in = by_target.get(here, [])
            mine = sum(1 for x in rows_in if x["status"] in LIVE)
            ctx = dict(rows_by_name=by_name, rows_in=rows_in, live_other=max(0, live.get(here, 0) - mine),
                       evidence=self._evidence.get(here, set()), plan_of=plan_of, shared=shared,
                       include_shared=settings.dav_include_shared, present=self._listed)
            self._one(s, ps, url, label, ctx)

    def _listed(self, t: str) -> set[str] | None:
        k = url_key(t)
        if k not in self._present:
            s = next(iter(self.sessions.values()))
            for p, sess in self.sessions.items():  # a session of the kind of that collection
                if any(url_key(fp.name) == k for fp in self.r.plans.get(p, {}).values()):
                    s = sess
                    break
            try:
                self._present[k] = {n.casefold() for n in self.r._on_session(s, lambda: s.resources(t))}
            except ConnectorError:
                self._present[k] = None
        return self._present[k]

    def _kept(self, url: str, label: str, why: str, quiet: bool = False) -> None:
        r = self.r
        if quiet:
            r.log(f"Collection '{label}' ({url}), created by mccb, left as it is: {why}")
            return
        w = (f"collection_kept: '{label}' ({url}) was created by an earlier run of mccb and no folder goes to it any "
             f"more, but it is not deleted: {why}")
        r.log(w, "warning")
        r.report["warnings"].append(w)
        r.report.setdefault("dav_kept", []).append({"url": url, "name": label, "why": why})

    def _one(self, s, ps: list[str], url: str, label: str, ctx: dict) -> None:
        r = self.r
        try:
            res = r._on_session(s, lambda: s.resources(url))
        except ConnectorError as e:
            self._kept(url, label, f"what it holds could not be listed ({e})")
            return
        v = judge(url, res, **ctx)
        if not v.delete:
            self._kept(url, label, v.why, v.quiet)
            return
        total = len(res)  # what it held when the run found it, moves included
        copies: dict[str, bytes] = {}
        where = self._backup(s, ps[0], url, res, copies)
        if isinstance(where, Exception):
            self._kept(url, label, f"its items could not be read to keep a copy of them ({where})")
            return
        moved = 0
        if v.moves:
            failed = self._moves(s, url, v.moves, copies, where)
            moved = len(v.moves) if failed is None else 0
            if failed:
                self._kept(url, label, failed)
                return
            for t in {t for _n, _r, t in v.moves}:  # listed again: what the server has now decides
                self._present.pop(url_key(t), None)
            try:
                res = r._on_session(s, lambda: s.resources(url))
            except ConnectorError as e:
                self._kept(url, label, f"what it holds could not be listed again after the moves ({e})")
                return
            ctx["rows_by_name"] = self._refreshed(ctx["rows_by_name"], v.moves)
            v2 = judge(url, res, **ctx)
            if not v2.delete or v2.moves:
                self._kept(url, label, v2.why or "items moved out of it are not all in their collection")
                return
            where = self._backup(s, ps[0], url, res, copies)
            if isinstance(where, Exception):
                self._kept(url, label, f"its items could not be read to keep a copy of them ({where})")
                return
        try:
            r._on_session(s, lambda: s.delete_collection(url))
        except ConnectorError as e:
            msg = f"the deletion failed ({e})" + (f"; a copy of its items is in {where}" if copies else "")
            self._kept(url, label, msg)
            return
        self._forget(url, ctx["rows_in"])
        reason = "+".join(v.reasons) or "moved"
        what = "; ".join(WHAT[x] for x in v.reasons if x in WHAT)
        r.log(f"Collection '{label}' ({url}) deleted: {total} item(s)"
              + (f", {what}" if what else "") + (f" ({moved} of them moved first)" if moved else "")
              + (f"; a copy of each is kept in {where}" if copies else ""))
        r.report.setdefault("dav_removed", []).append(
            {"url": url, "name": label, "items": total, "reason": reason, "moved": moved,
             "backup": where if copies else None})

    @staticmethod
    def _refreshed(by_name: dict[str, list[dict]], moves: list) -> dict[str, list[dict]]:
        """The rows as the moves left them (ok, in the collection they were written to)."""
        done = {id(row): t for _n, row, t in moves}
        out = {}
        for k, rows in by_name.items():
            out[k] = [dict(x, status="ok", target=done[id(x)], error=None) if id(x) in done else x for x in rows]
        return out

    def _backup(self, s, phase: str, url: str, res: dict[str, str], copies: dict[str, bytes]) -> str | Exception:
        """A copy of each object not copied yet, under mirror-trash/ (purged after MIRROR_TRASH_DAYS). The directory,
        or the error that stopped it."""
        r = self.r
        todo = [n for n in sorted(res) if n not in copies]
        try:
            got = r._on_session(s, lambda: s.contents(url, todo)) if todo else {}
            path = None
            for n, raw in got.items():
                path = mm.backup_copy(r.job_id, phase, url_key(url) + n, raw)
                copies[n] = raw
        except (ConnectorError, OSError) as e:
            return e
        return str(Path(path).parent) if path else str(settings.data_dir / mm.TRASH_DIR / f"job-{r.job_id}" / phase)

    def _moves(self, s, url: str, moves: list, copies: dict[str, bytes], where: str) -> str | None:
        """Each item deleted from `url` then written into the collection it goes to; at the first that cannot be,
        written back and the reason given (None: all moved)."""
        from .connector_runner import PHASE_OF_KIND
        r = self.r
        for name, row, t in moves:
            r.check()
            raw = copies.get(name)
            if raw is None:
                return f"'{name}' could not be read before being moved"
            phase = PHASE_OF_KIND[row["kind"]]
            dst = self.sessions.get(phase)
            if dst is None:
                return f"'{name}' has no session to be written into {t}"
            try:
                r._on_session(s, lambda: s.delete(url, name))
            except ConnectorError as e:
                return f"'{name}' could not be deleted from it to be written into {t} ({e})"
            breaker = getattr(r.targets[phase], "breaker", None)
            if breaker is not None:
                breaker.ok(t)  # its refusals in this run were caused by the copy just deleted
            try:
                dst.ensure()
                dst.put(t, OutgoingItem(phase, raw, "", name=name))
            except ConnectorError as e:
                try:
                    r._on_session(s, lambda: s.restore(url, name, raw))
                except ConnectorError as e2:
                    msg = (f"'{name}' could not be written into {t} ({e}) nor back into {url} ({e2}): its copy is in "
                           f"{where}")
                    r.log(msg, "error")
                    r.problems.append(msg)
                    return msg
                return f"{t} still refuses '{name}' once its copy here is deleted ({e}): written back here"
            db.ex("UPDATE items SET status='ok', error=NULL, target=?, target_etag=?, job_id=?, updated_at=? "
                  "WHERE id=?", (t, dst.written_version(t, name), r.job_id, time.time(), row["id"]))
            note = getattr(dst, "last_note", "") or ""
            r._item_log(f"Item {row['id']} ('{row.get('subject') or ''}'): moved from {url} into {t}, where its "
                        f"copy in {url} kept it out (one object per UID)" + (f"; {note}" if note else ""), "info")
            self.touched.add((phase, t))
        return None

    def _forget(self, url: str, rows_in: list[dict]) -> None:
        """Nothing recorded points at the deleted collection any more: its rows (items of shared folders left out,
        deleted by the mirror), the record of what moved out of it, the choices of earlier runs."""
        from .connector_runner import _pin_key
        r = self.r
        here = url_key(url)
        ids = [x["id"] for x in rows_in if x["status"] not in LIVE]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            db.ex(f"DELETE FROM items WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
        key = moved_key(r.account_id, r.pst_key)
        d = db.jload(db.kv_get(key), {}) or {}
        if isinstance(d, dict) and any(url_key(u) == here for u in d):
            db.kv_set(key, db.jdump({u: e for u, e in d.items() if url_key(u) != here}))
        for phase in ("calendar", "contacts", "tasks"):
            pk = _pin_key(r.account_id, r.pst_key, phase)
            pins = db.jload(db.kv_get(pk), {}) or {}
            if isinstance(pins, dict) and any(url_key(str(u)) == here for u in pins.values()):
                db.kv_set(pk, db.jdump({fk: u for fk, u in pins.items() if url_key(str(u)) != here}))
        for p in self.phases:
            r.targets[p].listed.pop(here, None)
            r.targets[p].labels.pop(url, None)
        if self._evidence is not None:
            self._evidence.pop(here, None)

    def _recheck(self) -> None:
        """The collections items were moved into: checked again, the report says what they hold now."""
        r = self.r
        for phase, t in sorted(self.touched):
            fks = [fk for fk, fp in r.plans.get(phase, {}).items() if url_key(fp.name) == url_key(t)]
            if fks:
                try:
                    r._check_target(self.sessions[phase], phase, fks, t)
                except ConnectorError as e:
                    r.log(f"{t} not checked again after the moves: {e}", "warning")


def clean(runner: "ConnectorJobRunner", phases: list[str]) -> None:
    Cleanup(runner, phases).run()
