"""What the Exchange connectors share: folder plans, keys, flags. The folder plan names a folder by a text the session
can read back on its own (`count_ours` is not told the item kind): "<kind>:@inbox", "<kind>:@default" or
"<kind>:<folder_key>"."""
from __future__ import annotations

import hashlib
import logging
import re

from .base import CALENDAR, CONTACTS, MAIL, TASKS, FolderPlan, SourceFolder
from .imap import _digest, folder_hash

log = logging.getLogger("mccb.connectors")

# custom property written on every item created in Exchange, the answer to "is it already there?"
IMPORT_GUID = "c5f2a7c0-3d64-4b7e-9a52-7d1f0e3b8a11"
IMPORT_PROP = "MccbImportId"
SPECIAL_WELL_KNOWN = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts", "trash": "deleteditems",
                      "junk": "junkemail"}
DEFAULT = "default"
# names of the default folder of a kind, in the source's language: its content goes to the default folder of the target
DEFAULT_NAMES = {
    CALENDAR: {"calendar", "calendrier", "kalender", "calendario", "agenda", "calendário", "default"},
    CONTACTS: {"contacts", "contact", "kontakte", "contactos", "contatti", "contactpersonen", "address book",
               "default", "personal"},
    TASKS: {"tasks", "tâches", "aufgaben", "tareas", "attività", "taken", "tarefas"},
}
FLAG_FLAGGED = 2
VERB_REPLY, VERB_REPLY_ALL, VERB_FORWARD = 102, 103, 104


def item_key(path: list[str], remote_id: str) -> str:
    """ASCII, 53 characters: the ids of Exchange are long, and per folder unless immutable."""
    return f"{folder_hash(path)}-{hashlib.sha1(remote_id.encode('utf-8')).hexdigest()[:32]}"  # noqa: S324 - identifier


def alt_key(message_id: str | None, when, size) -> str:
    """Same form as the IMAP source: the Message-ID, else the date and size."""
    return "m:" + _digest(message_id) if message_id else "d:" + _digest(f"{when}|{size}")


def flags_of(read: bool, flagged: bool = False, verb: int | None = None, draft: bool = False) -> list[str]:
    out = []
    if read:
        out.append("\\Seen")
    if flagged:
        out.append("\\Flagged")
    if draft:
        out.append("\\Draft")
    if verb in (VERB_REPLY, VERB_REPLY_ALL):
        out.append("\\Answered")
    elif verb == VERB_FORWARD:
        out.append("$Forwarded")
    return out


def wanted_flags(flags: list[str]) -> dict:
    low = {f.lower() for f in flags}
    verb = VERB_REPLY if "\\answered" in low else VERB_FORWARD if "$forwarded" in low else None
    return {"read": "\\seen" in low, "flagged": "\\flagged" in low, "draft": "\\draft" in low, "verb": verb}


def is_default_folder(f: SourceFolder) -> bool:
    if f.special == DEFAULT:
        return True
    return len(f.path) == 1 and f.path[0].strip().lower() in DEFAULT_NAMES.get(f.kind, ())


def plan(folders: list[SourceFolder], root: str | None) -> dict[str, FolderPlan]:
    from ..server.runner import folder_key
    out: dict[str, FolderPlan] = {}
    for f in folders:
        well_known = SPECIAL_WELL_KNOWN.get(f.special or "") if f.kind == MAIL else None
        if f.kind == MAIL and f.path == ["INBOX"]:
            well_known = "inbox"
        if f.kind != MAIL and is_default_folder(f):
            well_known = DEFAULT
        if well_known:
            name = f"{f.kind}:@{well_known}"
        else:
            name = f"{f.kind}:{folder_key(([root] if root else []) + list(f.path))}"
        out[folder_key(f.path)] = FolderPlan(name)
    return out


def parse_spec(spec: str) -> tuple[str, str | None, list[str]]:
    """(kind, well-known name or None, path)."""
    kind, _, rest = spec.partition(":")
    if rest.startswith("@"):
        return kind, rest[1:], []
    parts = re.split(r"(?<!\\)/", rest)  # folder_key escapes "/" and "\" in names
    return kind, None, [p.replace("\\/", "/").replace("\\\\", "\\") for p in parts if p]
