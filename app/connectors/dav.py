"""CalDAV and CardDAV. As targets: one collection per account and item kind, deterministic resource names, idempotent
PUT. As sources: every collection, each object as it is on the server."""
from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import unquote

from ..server.dav import DavClient, DavError, DavIncomplete, decode_object
from . import davfix, registry
from .base import (CALENDAR, CAP_DELETE, CAP_FOLDERS, CAP_INVENTORY, CAP_MIRROR, CAP_READBACK, CAP_UPDATE, CONTACTS,
                   DELETE_HARD, NOT_SENT, TASKS, ConnectorError, FetchedItem, FolderPlan, Listing, OutgoingItem,
                   RemoteItem, Source, SourceFolder, SourceItem, Target, TargetSession)
from .imap import folder_hash


def convert(e: DavError) -> ConnectorError:
    if e.status in (401, 403):
        return ConnectorError(str(e), ConnectorError.AUTH)
    if e.temporary:
        return ConnectorError(str(e), ConnectorError.TRANSIENT)
    return ConnectorError(str(e), ConnectorError.REJECTED)


class DavSession(TargetSession):
    ITEM_RETRY_DELAY = 2.0  # seconds before the one retry of an object the server answered 500 to

    def __init__(self, dav_kind: str, config: dict, secrets: dict, component: str = "VEVENT",
                 labels: dict[str, str] | None = None, listed: dict[str, str] | None = None,
                 breaker: "Breaker | None" = None):
        self.dav_kind = dav_kind  # caldav | carddav
        self.breaker = breaker if breaker is not None else Breaker()  # shared by the sessions of the target
        self.last_note = ""  # what the last put() had to do to get the object written ("" when nothing)
        self.renamed: dict[str, str] = {}  # collection created under another name than asked -> that name
        self._diag: dict | None = None  # the other collections and their names, for the diagnosis of a refusal
        self.labels = labels if labels is not None else {}  # collection to create -> its name (shared by the target)
        # url_key of every collection the plan saw listed -> its name (shared by the target): never created
        self.listed = listed if listed is not None else {}
        # set by the runner before plan_folders: {"previous": url_key of the collections the items of earlier runs
        # went to, "pinned": {folder key: collection an earlier run of the job chose}}
        self.history: dict = {}
        self.notes: list[str] = []  # the last plan, in words (logged by the runner)
        self.component = component  # VTODO: a calendar that accepts tasks
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None
        self._etags: dict[tuple[str, str], str] = {}  # (collection, name) -> etag seen by the last inventory()
        self._written: dict[tuple[str, str], str] = {}  # (collection, name) -> etag of what this session wrote
        self.keep_versions = False  # the mirror: an etag the PUT does not give back is read

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> None:
        self.close()  # DAV has no session: authentication is checked by the first request
        self.client = DavClient(self.config.get("login") or "", self.password,
                                bool(self.config.get("verify_tls", True)))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> DavClient:
        self.ensure()
        return self.client  # type: ignore[return-value]

    def _discovery(self) -> tuple[str, str | None]:
        """(default collection, created when there is none; home of the collections, None when unknown)."""
        cfg = self.config
        c = self._c()
        try:
            coll, home = c._discover(self.dav_kind, cfg.get("url") or None, cfg.get("email") or cfg.get("login") or "",
                                     cfg.get("discovery_host") or "", True, self.component)
        except DavError as e:
            raise convert(e) from e
        if coll is None:
            raise ConnectorError(f"no {self.dav_kind} collection found", ConnectorError.REJECTED)
        return coll, home

    def _collection(self) -> str:
        return self._discovery()[0]

    def _collections(self, default: str, home: str | None) -> list[Collection]:
        """The collections of the target that can take this kind of item, as the server lists them (all of them
        are kept in `listed`). A collection that grants no write is left out, unless none does (a server that
        says it wrongly would otherwise get a second copy of every collection)."""
        try:
            rows = self._c().listing(self.dav_kind, default, home, privileges=True)
        except DavError as e:
            raise convert(e) from e
        usable = []
        for url, label, comps, writable in rows:
            self.listed[url_key(url)] = label
            if self.dav_kind == "caldav" and comps and self.component not in comps:
                continue
            usable.append(Collection(url, label, writable))
        if not any(c.writable for c in usable):
            return usable
        return [c for c in usable if c.writable]

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        """Each source folder goes to an existing collection when there is one for it (choose_collection: the
        default folder of the source to the default collection of the target, the others to the collection of the
        same name), else to a new one of its name (created by ensure_folder when it has items; refused: its items
        are in error, never mixed into another collection).
        A name the target already has is never created a second time. `notes`: the plan, said in the job's log."""
        from ..server.runner import folder_key
        from .exchange_base import is_default_folder
        default, home = self._discovery()
        colls = self._collections(default, home)
        kind = item_kind(self.dav_kind, self.component)
        previous, pinned = self.history.get("previous", set()), self.history.get("pinned", {})
        out: dict[str, FolderPlan] = {}
        self.notes = []
        # no folder says it is the default one: a lone folder (one file, one calendar) is
        lone = len(folders) == 1 and not is_default_folder(folders[0])
        for f in folders:
            fk = folder_key(f.path)
            label = " / ".join(p.strip() for p in f.path if p.strip()) or "Import"
            is_default = lone or is_default_folder(f)
            hit, why = choose_collection(kind, label, is_default, colls, default, previous, pinned.get(fk))
            if hit is not None:
                out[fk] = FolderPlan(hit.url)
                self.notes.append(f"'{label}' -> existing '{hit.label}' ({hit.url}, {why})")
            elif is_default or home is None:  # home None: the URL names one collection, no home for others
                out[fk] = FolderPlan(default)
                self.notes.append(f"'{label}' -> {default} (the collection found by the discovery)")
            else:
                url = home + collection_slug(self.dav_kind, self.component, label)
                self.labels[url] = label
                out[fk] = FolderPlan(url, label=label)
                self.notes.append(f"'{label}' -> new collection '{label}' ({url})")
        return out

    def _default_label(self) -> str:
        return "Contacts" if self.dav_kind == "carddav" else "Tasks" if self.component == "VTODO" else "Calendar"

    def has_folder(self, name: str) -> bool | None:
        try:
            return self._c()._is_collection(self.dav_kind, name)
        except DavError:
            return None

    def ensure_folder(self, name: str, flag: str | None = None, label: str | None = None) -> None:
        """The collection `name` exists. One the server listed when the plan was made is never created (over
        itself or a second time): an error if it no longer opens. One to create is not created when the target now
        has a collection of that name at another URL (a second "Contacts"): refused, its items go to the fallback."""
        c = self._c()
        listed = self.listed.get(url_key(name))
        # without a label (a collection gone since the plan): the name it was planned under, never "PST Import"
        want = label or self.labels.get(name) or listed or self._default_label()
        try:
            if listed is not None:
                c.ensure_collection(self.dav_kind, name, self.component, want, create=False)
                return
            if c._probe(self.dav_kind, name) is None:
                return
            default, home = self._discovery()
            for url, other, _comps, _w in c.listing(self.dav_kind, default, home):
                if url_key(url) != url_key(name) and norm_name(other) == norm_name(want):
                    raise ConnectorError(f"the target already has a collection named '{other}' ({url}): a second "
                                         f"one is not created", ConnectorError.REJECTED)
            c.last_display = want
            c.ensure_collection(self.dav_kind, name, self.component, want)
            if getattr(c, "last_display", want) != want:
                self.renamed[name] = c.last_display
        except DavError as e:
            if e.status == 403:  # signed in already: a refused creation (MDaemon, shared homes), not a bad password
                raise ConnectorError(str(e), ConnectorError.REJECTED) from e
            raise convert(e) from e

    def put(self, folder: str, item: OutgoingItem) -> str | None:
        """Writes the resource, over one already there as mccb always did (a migration is idempotent: its items
        rows may be gone, its namespace new, while the resources it wrote are still on the target). The first PUT
        says If-None-Match: *, so that a write over an existing resource is known: it then goes with If-Match on
        the etag just read, and is marked "!" in written_version(): its owner is not proven, and the mirror never
        deletes nor updates it on that ground.

        A 500 is not taken for a server in trouble at once (SmarterMail gives it for some objects, every time): see
        _after_500. Items a collection keeps refusing while it answers trip the breaker of the target: the next ones
        of that collection are not sent (NOT_SENT), one in Breaker.PROBE still is."""
        name = item.name or ""
        text = decode_object(item.raw)
        self.last_note = ""
        held = self.breaker.blocked(folder)
        if held:
            raise ConnectorError(held, ConnectorError.REJECTED)
        try:
            try:
                self._write(folder, name, text)
            except DavError as e:
                if e.status != 500:
                    raise self._converted(e) from e
                self._after_500(folder, name, text, e)
        except ConnectorError as e:
            if e.kind == ConnectorError.REJECTED and not getattr(e, "explained", False):
                if self.breaker.failed(folder, str(e)):
                    e.args = (f"{e} (the {Breaker.LIMIT} last items sent to this collection were all refused: its "
                              "next items are not sent)",)
            raise
        self.breaker.ok(folder)
        return item.name

    @staticmethod
    def _converted(e: DavError) -> ConnectorError:
        if e.status in (404, 409):  # collection gone since the discovery: recreated then retried by the runner
            return ConnectorError(str(e), ConnectorError.FOLDER_MISSING)
        return convert(e)

    def _write(self, folder: str, name: str, text: str) -> None:
        c = self._c()
        mark = ""
        try:
            c.put(folder, name, text, self.dav_kind, if_none_match=True)
        except DavError as e:
            if e.status != 412:
                raise
            mine = self._written.get((folder, name))
            mark = "" if mine and not mine.startswith("!") else "!"  # written by this very session: still ours
            old = c.etag_of(folder, name)
            c.put(folder, name, text, self.dav_kind, if_match=old or None)
        self._remember(folder, name, mark)

    def _after_500(self, folder: str, name: str, text: str, first: DavError) -> None:
        """The server answered 500 to this object. One retry a moment later (without If-None-Match when the resource
        is not there: some servers answer 500 to the condition; with If-Match over it when it is, as after a 412).
        500 again: when the collection does not answer either, the server is in trouble (TRANSIENT: the runner
        retries as usual); when it does, the object is the problem: an object of the same UID elsewhere in the
        account is looked for (a clear reason, REJECTED), else the object is written in a plainer form (davfix:
        `last_note` says which), else REJECTED at once instead of the long retries of a server in trouble.

        Learned per collection (Breaker): the form that got an object written is tried first for the next ones, at
        once (a server refusing every card with a website: one PUT more per card, not a dozen requests and seconds);
        once a few objects were refused in every form, the moment before the retry is not waited for any more."""
        c = self._c()
        what = self._what()
        forms = davfix.forms(self.dav_kind, text)
        known = self.breaker.remedy(folder)
        if not (known and any(f[0] == known for f in forms)) and not self.breaker.hopeless(folder):
            time.sleep(self.ITEM_RETRY_DELAY)  # a moment, unless this collection's refusals are known by now
        try:
            state = c.state_of(folder, name)
        except DavError as e:
            raise ConnectorError(f"{first} (then PROPFIND of the item -> {e})", ConnectorError.TRANSIENT) from e
        mine = self._written.get((folder, name))
        mark = "!" if state is not None and not (mine and not mine.startswith("!")) else ""

        def attempt(body: str) -> bool:
            try:
                if state is None:
                    c.put(folder, name, body, self.dav_kind)
                else:
                    c.put(folder, name, body, self.dav_kind, if_match=state or None)
            except DavError as e:
                if e.status != 500:
                    raise self._converted(e) from e
                return False
            self._remember(folder, name, mark)
            return True

        def written(label: str, body: str, notes: list[str]) -> bool:
            if not attempt(body):
                return False
            self.breaker.learn(folder, label)
            self.last_note = (f"the server answered HTTP 500 to this {what} as it is: written in a {label} form"
                              + (f" ({'; '.join(notes)})" if notes else ""))
            return True

        if known and state is None:  # the form that got the ones before written: first (never over a resource)
            for label, body, notes in forms:
                if label == known and written(label, body, notes):
                    return
        if attempt(text):
            return
        status = c._probe(self.dav_kind, folder)
        if status is not None:
            if status == 404:
                raise ConnectorError(f"{first} (the collection is gone)", ConnectorError.FOLDER_MISSING)
            raise ConnectorError(f"{first}, twice; the collection does not answer either (PROPFIND -> "
                                 f"{f'HTTP {status}' if status else 'no answer'}): the server is in trouble",
                                 ConnectorError.TRANSIENT)
        why = self._uid_conflict(folder, name, text)
        if why:
            err = ConnectorError(f"{first}: {why}", ConnectorError.REJECTED)
            err.explained = True  # type: ignore[attr-defined]  # a reason of its own: not the collection's fault
            raise err
        for label, body, notes in forms:
            if label != known and written(label, body, notes):
                return
        self.breaker.refused(folder)
        raise ConnectorError(f"{first}: the server refuses this {what} (HTTP 500 again a moment later"
                             + (f", and for its {' and '.join(f[0] for f in forms)} form" if forms else "")
                             + ") while the collection answers", ConnectorError.REJECTED)

    def _what(self) -> str:
        return "contact" if self.dav_kind == "carddav" else "task" if self.component == "VTODO" else "event"

    def _uid_conflict(self, folder: str, name: str, text: str) -> str | None:
        """Why the server may refuse this object: one of the same UID already in the account (SmarterMail keeps one
        per UID: the copies an earlier run made in a second "Contacts"), None when none is found. Best effort: a
        request that fails says nothing. An object is named only once read (GET) with that very UID: a server may
        answer an addressbook-query with every card of the collection (MDaemon ignores its filter), which a query
        for a UID no object has shows first (that collection is then not asked)."""
        uid = davfix.object_uid(text)
        c = self._c()
        what = self._what()
        here = url_key(folder)
        word = "address book" if self.dav_kind == "carddav" else "calendar"
        if self._diag is None:
            try:
                default, home = self._discovery()
                rows = c.listing(self.dav_kind, default, home)
            except (ConnectorError, DavError):
                rows = []
            self._diag = {"colls": [(u, label) for u, label, _c, _w in rows], "names": {}, "query": True}
        diag = self._diag
        mine = self.listed.get(here) or self.labels.get(folder) or ""

        def said(url: str, label: str, ref: str) -> str:
            twin = made_by_mccb(url) or (mine and norm_name(label) == norm_name(mine))
            return (f"a {what} with the UID '{uid or name}' already exists in the {word} '{label}' ({ref}): the server "
                    f"keeps one {what} per UID in the account. "
                    + (f"'{label}' looks like a copy an earlier run made: check it, delete that {word} in the webmail, "
                       "then rerun" if twin else
                       f"Delete that copy (or '{label}' if it is a duplicate {word}) in the webmail, then rerun"))

        others = [(u, label) for u, label in diag["colls"] if url_key(u) != here]

        def holds(url: str, res: str) -> bool:
            """The resource really has this UID (GET): never blamed on a name or on a server's answer alone."""
            try:
                raw = c.get_bytes(url, res)
            except DavError:
                return False
            return raw is not None and (uid is None or davfix.object_uid(raw) == uid)

        for url, label in others:  # the same resource name (mccb names an object after its UID)
            names = diag["names"].get(url)
            if names is None:
                try:
                    names = {n.casefold() for n in c.names(url)}
                except DavError:
                    names = set()
                diag["names"][url] = names
            if name.casefold() in names and holds(url, name):
                return said(url, label, url + name)
        if not uid or not diag["query"]:
            return None
        filters = diag.setdefault("filters", {})
        for url, label in [(folder, mine or folder), *others]:  # the same UID under another name
            try:
                if url_key(url) not in filters:  # a UID no object has: a server that ignores the filter says so
                    filters[url_key(url)] = not c.find_uid(self.dav_kind, url, f"mccb-none-{uuid.uuid4().hex}",
                                                           self.component)
                if not filters[url_key(url)]:
                    continue  # it lists every object whatever the UID asked (MDaemon): it tells nothing
                hrefs = c.find_uid(self.dav_kind, url, uid, self.component)
            except DavError:
                diag["query"] = False  # the server does not do it: not asked again for every item
                return None
            hrefs = [h for h in hrefs if not (url_key(url) == here and _last(h).casefold() == name.casefold())]
            hrefs = [h for h in hrefs[:5] if holds(h.rsplit("/", 1)[0] + "/", _last(h))]
            if hrefs:
                if url_key(url) == here:
                    return (f"a {what} with the UID '{uid}' is already in this {word} under another name ({hrefs[0]}): "
                            "the server keeps one per UID. Delete that copy in the webmail, then rerun")
                return said(url, label, hrefs[0])
        return None

    def _remember(self, folder: str, name: str, mark: str = "") -> None:
        """Keeps the etag of a write. Without one in the answer, it is read (PROPFIND) only for the mirror
        (keep_versions): a normal migration does not pay a request more per item."""
        c = self._c()
        et = getattr(c, "last_etag", "") or ""
        if not et and self.keep_versions:
            try:
                et = c.etag_of(folder, name)
            except DavError:
                et = ""
        if et or mark:
            self._written[(folder, name)] = mark + et
        else:
            self._written.pop((folder, name), None)

    def written_version(self, folder: str, ref: str) -> str | None:
        """The etag of what this session last wrote to `ref`, "!" first when it was written over a resource that
        was already there (None: unknown)."""
        return self._written.get((folder, ref))

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        if not item.name:
            return []
        try:
            return [item.name] if self._c().exists(folder, item.name) else []
        except DavError as e:
            raise convert(e) from e

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        try:  # only OUR resources count, not pre-existing content; some servers do not keep the case of a name
            there = {n.casefold() for n in self._c().names(folder)}
        except DavError as e:
            raise convert(e) from e
        return len(there & {n.casefold() for n in names or ()})

    def missing_ours(self, folder: str, names: set[str]) -> set[str]:
        """Those of `names` the collection does not list (the check says which items are missing)."""
        try:
            there = {n.casefold() for n in self._c().names(folder)}
        except DavError as e:
            raise convert(e) from e
        return {n for n in names if n.casefold() not in there}

    def read(self, folder: str, remote_id: str) -> bytes | None:
        try:
            return self._c().get_bytes(folder, remote_id)
        except DavError as e:
            raise convert(e) from e

    # ---- mirror: only the resources named in `names` (ours), with If-Match on what the inventory saw
    def inventory(self, folder: str, namespace: str, names: set[str] | None = None) -> dict[str, list[RemoteItem]]:
        try:
            found = self._c().names_etags(folder, strict=True)  # cut short: an error, not a smaller collection
        except DavError as e:
            if e.status == 404:
                return {}
            raise convert(e) from e
        out = {}
        wanted = {n.casefold(): n for n in names or ()}  # some servers do not keep the case of a name
        for name, et in found.items():
            asked = wanted.get(name.casefold())
            if asked is not None:
                self._etags[(folder, name)] = et
                out[asked] = [RemoteItem(name, version=et or None)]
        return out

    def delete_mode(self, folder: str) -> str:
        return DELETE_HARD  # no trash in DAV: the runner keeps a copy (read()) before deleting

    def delete(self, folder: str, ref: str, expected_import_id: str | None = None) -> str:
        if expected_import_id is not None and expected_import_id.casefold() != ref.casefold():
            raise ConnectorError(f"{ref} is not {expected_import_id}: not deleted", ConnectorError.REJECTED)
        try:
            sc = self._c().delete_item(folder, ref, self._etags.get((folder, ref)))
        except DavError as e:
            raise convert(e) from e
        if sc == 412:
            raise ConnectorError(f"{ref} changed on the target since it was listed: not deleted",
                                 ConnectorError.REJECTED)
        self._etags.pop((folder, ref), None)
        return DELETE_HARD

    def update(self, folder: str, ref: str, item: OutgoingItem) -> str | None:
        """PUT over our resource, with If-Match on the etag the inventory saw: changed on the target since (412),
        it is not overwritten (a temporary error: the next pass sees it again)."""
        etag = self._etags.get((folder, ref))
        try:
            self._c().put(folder, ref, decode_object(item.raw), self.dav_kind, if_match=etag or None)
        except DavError as e:
            if e.status in (404, 409):
                raise ConnectorError(str(e), ConnectorError.FOLDER_MISSING) from e
            raise convert(e) from e
        self._remember(folder, ref)  # the etag of the inventory stays: a second write without one gets 412
        return ref

    # ---- collections an earlier run left behind (app/server/davcleanup.py)
    def collections_now(self) -> list[tuple[str, str]]:
        """[(url, display name)] of every collection of this kind of the account, as the server lists them now ([]
        when the URL names one collection: there is no home to list)."""
        default, home = self._discovery()
        if home is None:
            return []
        try:
            rows = self._c().listing(self.dav_kind, default, home)
        except DavError as e:
            raise convert(e) from e
        return [(url, label) for url, label, _comps, _w in rows]

    def resources(self, folder: str) -> dict[str, str]:
        """{name: etag} of every resource of the collection, whoever wrote it. Complete or an error: a listing the
        server says it cut short raises. The etags are kept: delete() of one of them goes with If-Match."""
        try:
            found = self._c().names_etags(folder, strict=True)
        except DavError as e:
            raise convert(e) from e
        for name, et in found.items():
            if et:
                self._etags[(folder, name)] = et
        return found

    def contents(self, folder: str, names: list[str]) -> dict[str, bytes]:
        """{name: object as the server gives it} of these resources (one GET each: the exact bytes). A resource gone
        since its listing is left out."""
        c = self._c()
        out: dict[str, bytes] = {}
        try:
            for n in names:
                raw = c.get_bytes(folder, n)
                if raw is not None:
                    out[n] = raw
        except DavError as e:
            raise convert(e) from e
        return out

    def restore(self, folder: str, name: str, raw: bytes) -> None:
        """Writes an object back where it was (after a move that failed), without condition: it was just deleted."""
        try:
            self._c().put(folder, name, decode_object(raw), self.dav_kind)
        except DavError as e:
            raise convert(e) from e

    def delete_collection(self, url: str) -> str:
        """Deletes the collection `url` with everything in it. "deleted", or "gone" when it was not there (404). A
        500 is tried once more a moment later, unless the collection went anyway (SmarterMail answers 500 to some
        requests it carried out). Gone is checked afterwards: a 2xx that leaves the collection there (a multistatus
        naming members it could not delete) is an error, never taken for done."""
        c = self._c()
        try:
            try:
                sc = c.delete_collection(url)
            except DavError as e:
                if e.status != 500:
                    raise
                time.sleep(self.ITEM_RETRY_DELAY)
                if c._probe(self.dav_kind, url) in (404, 410):
                    return "deleted"
                sc = c.delete_collection(url)
        except DavError as e:
            if e.status in (403, 405, 409):  # signed in already: a refusal of this deletion, not a bad password
                raise ConnectorError(f"the server refuses to delete it ({e})", ConnectorError.REJECTED) from e
            raise convert(e) from e
        if sc == 404:
            return "gone"
        state = c._probe(self.dav_kind, url)
        if state is None:
            raise ConnectorError(f"DELETE {url} -> HTTP {sc}, but the collection is still there",
                                 ConnectorError.REJECTED)
        if state in (404, 410, 207):  # 207: what answers there is no such collection any more
            return "deleted"
        raise ConnectorError(f"DELETE {url} -> HTTP {sc}, then the collection does not answer (PROPFIND -> "
                             f"{f'HTTP {state}' if state else 'no answer'}): not known whether it is gone",
                             ConnectorError.TRANSIENT)


class Breaker:
    """Items a collection refuses one after the other while it answers (the server is up, the PUTs fail): after
    LIMIT of them in a row, the next items of that collection are not sent, their error says why (NOT_SENT); one in
    PROBE still is, and a success closes the breaker. A server in trouble never trips it: its errors are temporary,
    retried as usual. Shared by the sessions of a target, for one run."""
    LIMIT = 20
    PROBE = 25

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.forms: dict[str, str] = {}  # collection -> the davfix form that got an object written after a 500
        self.hopeless_n: dict[str, int] = {}  # collection -> objects refused in every form

    def blocked(self, folder: str) -> str | None:
        with self.lock:
            st = self.state.get(url_key(folder))
            if st is None or st["streak"] < self.LIMIT:
                return None
            st["held"] += 1
            if st["held"] % self.PROBE == 0:
                return None  # one still goes: the cause may be gone
            st["skipped"] += 1
            return (f"{NOT_SENT}not sent: the {st['streak']} items before it in this collection were all refused while "
                    f"it answers; rerun once the cause is fixed (last refusal: {st['why']})")

    def failed(self, folder: str, why: str) -> bool:
        """One more refusal; True when it trips the breaker."""
        with self.lock:
            st = self.state.setdefault(url_key(folder), {"streak": 0, "held": 0, "skipped": 0, "reported": 0,
                                                         "why": ""})
            st["streak"] += 1
            st["why"] = why[:400]
            return st["streak"] == self.LIMIT

    HOPELESS = 3  # objects of a collection refused in every form, after which no moment is waited before a retry

    def remedy(self, folder: str) -> str | None:
        """The form (davfix) that last got an object of this collection written after a 500."""
        with self.lock:
            return self.forms.get(url_key(folder))

    def learn(self, folder: str, label: str) -> None:
        with self.lock:
            self.forms[url_key(folder)] = label

    def refused(self, folder: str) -> None:
        """One more object refused in every form while the collection answers."""
        with self.lock:
            self.hopeless_n[url_key(folder)] = self.hopeless_n.get(url_key(folder), 0) + 1

    def hopeless(self, folder: str) -> bool:
        with self.lock:
            return self.hopeless_n.get(url_key(folder), 0) >= self.HOPELESS

    def ok(self, folder: str) -> None:
        with self.lock:
            st = self.state.get(url_key(folder))
            if st is not None:
                st["streak"] = st["held"] = 0

    def summary(self) -> list[tuple[str, int, str]]:
        """[(collection, items not sent since the last summary, last refusal)]."""
        out = []
        with self.lock:
            for k, st in self.state.items():
                n = st["skipped"] - st["reported"]
                if n > 0:
                    st["reported"] = st["skipped"]
                    out.append((k, n, st["why"]))
        return out


def _last(href: str) -> str:
    return unquote(href.rstrip("/").rsplit("/", 1)[-1])


def collection_slug(dav_kind: str, component: str, label: str) -> str:
    """Path segment of a collection created for a source folder: readable, stable across runs, distinct per kind
    (CalDAV and CardDAV often share their home)."""
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "folder"
    digest = hashlib.sha1(f"{dav_kind}:{component}:{label}".encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"mccb-{text}-{digest}/"


# ------------------------------------------------------------------ which collection for which source folder
# lists some servers keep as address books (SmarterMail), under the name of the account's language: the one of the
# source goes to the one of the target, never to a second list of another name
SENDER_LISTS = (
    frozenset({"allowed senders", "trusted senders", "safe senders", "expéditeurs autorisés",
               "expéditeurs approuvés", "erlaubte absender", "vertrauenswürdige absender", "remitentes permitidos",
               "mittenti consentiti", "mittenti attendibili", "toegestane afzenders", "remetentes permitidos"}),
    frozenset({"blocked senders", "expéditeurs bloqués", "blockierte absender", "gesperrte absender",
               "remitentes bloqueados", "mittenti bloccati", "geblokkeerde afzenders", "remetentes bloqueados"}),
)
_MADE_BY_MCCB = re.compile(r"/(?:mccb-[a-z0-9-]*|import-pst-[a-z]+)/?$")


def norm_name(label: str) -> str:
    """A display name as compared: case, accents composition and spaces do not count."""
    return " ".join(unicodedata.normalize("NFC", label or "").split()).casefold()


def url_key(url: str) -> str:
    """A collection URL as compared: the servers do not all encode the same characters."""
    return unquote(url if url.endswith("/") else url + "/")


def item_kind(dav_kind: str, component: str) -> str:
    return CONTACTS if dav_kind == "carddav" else TASKS if component == "VTODO" else CALENDAR


def is_default_name(kind: str, label: str) -> bool:
    from .exchange_base import DEFAULT_NAMES
    return norm_name(label) in {norm_name(n) for n in DEFAULT_NAMES.get(kind, ())}


def made_by_mccb(url: str) -> bool:
    """A collection mccb created (collection_slug, or "import-pst-*" of the discovery)."""
    return bool(_MADE_BY_MCCB.search(url_key(url).lower()))


def default_index(kind: str, labels: list[str]) -> int | None:
    """Which of these collections (in the order the server lists them) is the default one: the only one, else the
    first with the default name of its kind in some language ("Contacts", "Calendrier", "Kalender"...), else the
    first. None when there is none."""
    if not labels:
        return None
    return next((i for i, label in enumerate(labels) if is_default_name(kind, label)), 0)


@dataclass
class Collection:
    url: str
    label: str
    writable: bool = True


def choose_collection(kind: str, label: str, default: bool, colls: list[Collection], discovered: str | None,
                      previous: set[str] | frozenset = frozenset(), pinned: str | None = None
                      ) -> tuple[Collection | None, str]:
    """The existing collection a source folder goes to, None: one to create. (collection, why).

    `colls`: the collections of the target that can take these items, in the order the server lists them;
    `discovered`: the one the discovery found first; `previous`: url_key of the collections the items of earlier runs
    went to; `pinned`: the collection an earlier run of this job chose for this folder (kept while it is there).

    Candidates: the collections of the same name (whatever the case); the list of the same role (SENDER_LISTS); for
    the default folder of the source, also every collection with a default name, then the discovered one. Among
    them, never one that duplicates another candidate: a collection mccb created next to one of the server's own, or
    a collection holding the items of an earlier run next to one of the same name that holds none (the second
    "Contacts" an earlier run made). Then one already used by an earlier run, then the closest name, then the order
    of the server."""
    if pinned is not None:
        hit = next((c for c in colls if url_key(c.url) == url_key(pinned)), None)
        if hit is not None:
            return hit, "chosen by an earlier run"
    name = norm_name(label)
    group = next((g for g in SENDER_LISTS if name in g), frozenset())
    cands: list[tuple[int, int, Collection]] = []
    for i, c in enumerate(colls):
        n = norm_name(c.label)
        if n == name:
            tier = 0
        elif n in group or (default and is_default_name(kind, c.label)):
            tier = 1
        elif default and discovered is not None and url_key(c.url) == url_key(discovered):
            tier = 2
        else:
            continue
        cands.append((tier, i, c))
    if not cands:
        return None, "no collection of that name"
    prev = {url_key(u) for u in previous}

    def extra(c: Collection) -> bool:
        if made_by_mccb(c.url) and any(not made_by_mccb(o.url) for _t, _i, o in cands):
            return True
        return url_key(c.url) in prev and any(
            o is not c and norm_name(o.label) == norm_name(c.label) and url_key(o.url) not in prev
            for _t, _i, o in cands)

    tier, _i, best = min(cands, key=lambda e: (extra(e[2]), url_key(e[2].url) not in prev, e[0], e[1]))
    why = ("same name" if tier == 0 else "same list" if group and norm_name(best.label) in group
           else "default collection" if tier == 1 else "first collection of the server")
    return best, why


def default_collection(client: DavClient, dav_kind: str, template: str | None, email: str, host: str,
                       previous: set[str] | frozenset = frozenset(), component: str = "VEVENT") -> str:
    """The default collection of an account (the PST path: every folder of a kind goes there): the one an earlier
    run used, else the one with the default name of its kind, else the first the discovery finds (created when
    there is none). Raises DavError."""
    coll, home = client._discover(dav_kind, template, email, host, True, component)
    if coll is None:
        raise DavError(f"no {dav_kind} collection found")
    colls = [Collection(url, label, w) for url, label, comps, w in client.listing(dav_kind, coll, home, privileges=True)
             if not (dav_kind == "caldav" and comps and component not in comps)]
    if any(c.writable for c in colls):
        colls = [c for c in colls if c.writable]
    hit, _why = choose_collection(item_kind(dav_kind, component), "", True, colls, coll, previous)
    return hit.url if hit is not None else coll


_UID = re.compile(rb"^UID(?:;[^:\r\n]*)?:([^\r\n]*)", re.M | re.I)
_SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+=-]{0,199}")


def uid_name(raw: bytes, ext: str) -> str | None:
    """"<UID>.<ext>" when the UID of the object is safe as a file name. Some servers (MDaemon) store an object under
    that name whatever the URL of the PUT: naming it so ourselves keeps our resources recognisable on every server."""
    unfolded = re.sub(rb"\r?\n[ \t]", b"", raw)
    m = _UID.search(unfolded)
    if not m:
        return None
    uid = m.group(1).decode("utf-8", "replace").strip()
    return f"{uid}.{ext}" if _SAFE_UID.fullmatch(uid) else None


class DavTarget(Target):
    capabilities = frozenset({CAP_READBACK, CAP_INVENTORY, CAP_DELETE, CAP_UPDATE})
    names_resources = True  # an item IS its resource name: two items of one name are one resource
    keep_versions = False   # set by a mirror job: every write's etag is read when the PUT does not give it
    dav_kind = "caldav"
    component = "VEVENT"

    def resource_name(self, raw: bytes, default: str) -> str:
        return uid_name(raw, default.rsplit(".", 1)[-1] if "." in default else
                        ("vcf" if self.dav_kind == "carddav" else "ics")) or default

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.labels: dict[str, str] = {}
        self.listed: dict[str, str] = {}
        self.breaker = Breaker()

    def session(self) -> DavSession:
        session = DavSession(self.dav_kind, self.config, self.secrets, self.component, self.labels, self.listed,
                             self.breaker)
        session.keep_versions = self.keep_versions
        return session

    def for_item(self, item: str) -> "DavTarget":
        """The target to use for this item kind (wiring.py): tasks need a calendar that accepts VTODO."""
        return self


class CardDavTarget(DavTarget):
    dav_kind = "carddav"


class CalDavTasksTarget(DavTarget):
    dav_kind = "caldav"
    component = "VTODO"


class CalDavTarget(DavTarget):
    dav_kind = "caldav"

    def for_item(self, item: str) -> DavTarget:
        return CalDavTasksTarget(self.config, self.secrets) if item == TASKS else self


# ------------------------------------------------------------------ source
# how SmarterMail names the collections other accounts share with this one, and the public ones of the domain
SHARED = "shared"  # SourceFolder.special of a collection the account does not own (skipped)
_SHARED_NAME = re.compile(r"^\[([^\]\s@]+@[^\]\s]+)\]\s*-\s*\S")
_PUBLIC_NAME = re.compile(r"^\[(?:public|public folders?|öffentlich|publique?s?|pubblic[oa]|público)\]\s*-\s*(.*)$",
                          re.I)


def foreign_collections(rows: list[tuple[str, str, list[str]]], owners: dict, email: str) -> dict[str, str]:
    """{url: why} of the collections of a source this account does not own. What the server says first (a
    calendarserver.org "shared" mark, a DAV:owner other than the account's principal), then the names SmarterMail
    gives them ("[compta@kmy.ch] - Calendrier", "[Public] - kmy.ch/Contacts"). The owners are believed only when
    the server gives the account's own principal as the owner of at least one collection, and never when they
    would leave none of the collections to the account: a server that says it another way loses nothing."""
    cols = owners.get("collections") or {}
    me = {url_key(p) for p in owners.get("principals") or ()}
    comparable = any(url_key(c["owner"]) in me for c in cols.values() if c.get("owner"))
    by_server: dict[str, str] = {}
    for url, _label, _comps in rows:
        c = cols.get(url) or cols.get(url.rstrip("/") + "/") or {}
        if c.get("shared"):
            by_server[url] = "shared with this mailbox by another account: migrated with its owner's mailbox"
        elif comparable and c.get("owner") and url_key(c["owner"]) not in me:
            by_server[url] = f"owned by another account ({c['owner']}): migrated with its owner's mailbox"
    if len(by_server) == len(rows):
        by_server = {}
    out: dict[str, str] = {}
    for url, label, _comps in rows:
        m = _SHARED_NAME.match(label or "")
        p = _PUBLIC_NAME.match(label or "")
        if m and m.group(1).casefold() != (email or "").casefold():
            out[url] = f"shared from {m.group(1)}: migrated with that mailbox"
        elif p:
            where = p.group(1).split("/")[0].strip()
            out[url] = (f"public folder{f' of {where}' if where else ''}: it belongs to the domain, not to this "
                        "mailbox")
        elif url in by_server:
            out[url] = by_server[url]
    return out


_SUMMARY = re.compile(rb"^(?:SUMMARY|FN)[^:\r\n]*:(.*)$", re.M)


class DavSource(Source):
    """Reads every collection of the account. Objects are copied as they are: nothing is parsed but the kind."""
    dav_kind = "caldav"

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None

    def open(self) -> None:
        self.close()
        self.client = DavClient(self.config.get("login") or "", self.password, bool(self.config.get("verify_tls", True)))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> DavClient:
        if self.client is None:
            raise ConnectorError("DAV source not connected", ConnectorError.TRANSIENT)
        return self.client

    def folders(self) -> list[SourceFolder]:
        """Every collection, each once per kind it holds. One this account does not own (shared with it by another
        mailbox, a public folder of the domain) is listed with `skip`: it is migrated with its owner's mailbox, not
        a second time with each mailbox it is shared with (DAV_INCLUDE_SHARED=1: migrated as the others)."""
        from ..server.config import settings
        cfg = self.config
        email = cfg.get("email") or cfg.get("login") or ""
        try:
            rows, owners = self._c().source_collections(self.dav_kind, cfg.get("url") or None, email,
                                                        cfg.get("discovery_host") or "")
        except DavError as e:
            raise convert(e) from e
        foreign = foreign_collections(rows, owners, email)
        out: list[SourceFolder] = []
        for url, label, comps in rows:
            kinds = [CONTACTS] if self.dav_kind == "carddav" else [
                k for k, c in ((CALENDAR, "VEVENT"), (TASKS, "VTODO")) if not comps or c in comps]
            why = None if settings.dav_include_shared else foreign.get(url)
            for kind in kinds:  # special "shared": the copies an earlier version made of it are removed (davcleanup)
                out.append(SourceFolder(path=[label], kind=kind, ref=url, skip=why, special=SHARED if why else None))
        # the default one of each kind: by its name, else the first (default_index); never a list such as
        # "Allowed Senders" because the server lists it first, nor a collection of another account
        for kind in (CALENDAR, TASKS, CONTACTS):
            mine = [f for f in out if f.kind == kind and not f.skip]
            i = default_index(kind, [f.path[0] for f in mine])
            if i is not None:
                mine[i].special = "default"
        return out

    def _key(self, folder: SourceFolder, name: str) -> str:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:32]  # noqa: S324 - identifier, not security
        return f"{folder_hash(folder.path)}-{digest}"

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        """Every resource with its etag (`version`: the mirror updates an item whose etag changed)."""
        try:
            found = self._c().names_etags(folder.ref)
        except DavError as e:
            raise convert(e) from e
        for name in sorted(found):
            yield SourceItem(key=self._key(folder, name), ref=name, version=found[name] or None)

    # ---- mirror: PROPFIND and GET only
    def _listed(self, folder: SourceFolder, found: dict[str, str]) -> list[SourceItem]:
        return [SourceItem(key=self._key(folder, n), ref=n, version=found[n] or None) for n in sorted(found)]

    def mirror_listing(self, folder: SourceFolder) -> Listing:
        """The PROPFIND of the collection; complete unless the server says in it that it cut it short (507)."""
        try:
            found = self._c().names_etags(folder.ref, strict=True)
        except DavIncomplete as e:
            return Listing(self._listed(folder, e.partial or {}), complete=False, problem=str(e))
        except DavError as e:
            raise convert(e) from e
        return Listing(self._listed(folder, found))

    def exists(self, folder: SourceFolder, keys: list[str], refs: dict | None = None) -> dict[str, bool]:
        """GET of each resource whose name was recorded. A key without a recorded name cannot be read by itself
        (the key is a digest of the name) and another listing would only repeat the one that missed it: answered
        present (kept), as EWS and Graph do."""
        out = {k: True for k in keys}
        named = {k: r for k, r in (refs or {}).items() if isinstance(r, str) and k in out}
        try:
            for k, name in named.items():
                out[k] = self._c().get(folder.ref, name) is not None
        except DavError as e:
            raise convert(e) from e
        return out

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        try:
            text = self._c().get(folder.ref, item.ref)
        except DavError as e:
            raise convert(e) from e
        if text is None:
            return None
        raw = text.encode("utf-8")
        wanted = {CALENDAR: b"BEGIN:VEVENT", TASKS: b"BEGIN:VTODO", CONTACTS: b"BEGIN:VCARD"}[folder.kind]
        if wanted not in raw.upper():
            return None  # a collection holding events and tasks is listed once per kind
        m = _SUMMARY.search(raw)
        return FetchedItem(raw=raw, subject=m.group(1).decode("utf-8", "replace").strip() if m else "")


class CalDavSource(DavSource):
    dav_kind = "caldav"


class CardDavSource(DavSource):
    dav_kind = "carddav"


def identity(config: dict) -> str:
    return f"{config.get('url') or config.get('discovery_host') or ''}:{config.get('login', '')}"


_FIELDS = (
    registry.Field("url", "URL", "url", help="Empty: discovery from the host below. May contain {email}, {user}, "
                                             "{domain}, {login}."),
    registry.Field("discovery_host", "Host for discovery"),
    registry.Field("email", "Email address"),
    registry.Field("login", "Login", required=True),
    registry.Field("verify_tls", "Verify the TLS certificate", "boolean", default=True),
    registry.Field("password", "Password", "password", required=True, secret=True),
)

registry.register(registry.ConnectorSpec(
    kind="caldav", label="CalDAV", description="Calendars and tasks on a CalDAV server.",
    source=registry.Role(CalDavSource, (CALENDAR, TASKS), frozenset({CAP_FOLDERS, CAP_MIRROR}), _FIELDS, identity),
    target=registry.Role(CalDavTarget, (CALENDAR, TASKS), CalDavTarget.capabilities, _FIELDS, identity)))
registry.register(registry.ConnectorSpec(
    kind="carddav", label="CardDAV", description="Address books on a CardDAV server.",
    source=registry.Role(CardDavSource, (CONTACTS,), frozenset({CAP_FOLDERS, CAP_MIRROR}), _FIELDS, identity),
    target=registry.Role(CardDavTarget, (CONTACTS,), CardDavTarget.capabilities, _FIELDS, identity)))
