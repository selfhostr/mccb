"""Folder mapping: which target folder a source folder lands in.

Three layers, from the most to the least specific:

1. rules of the account, then rules of the server: `{"source": pattern, "target": "@sent" | "Some/Path"}`;
2. built-in names (Sent Items, Deleted Items, Junk, Drafts and their translations in the nine interface languages),
   for the folders the source did not already flag;
3. the special folders the source declared (SPECIAL-USE, PST).

The mapping only changes the NAME of the target folder, never the key of an item (`folder_key` of the source
folder), so it cannot make an imported item look new. See docs/planning.md for what a change of mapping does to
items already imported.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Callable

ROLES = ("inbox", "sent", "drafts", "trash", "junk")

# Lower-cased names (str.casefold) of the standard folders in the nine interface languages
# (de, en, es, fr, it, pt, pt-BR, ru, zh-Hans) and in the usual clients' defaults.
DEFAULT_NAMES: dict[str, frozenset[str]] = {
    "inbox": frozenset({
        "inbox", "boîte de réception", "boite de reception", "posteingang", "bandeja de entrada",
        "posta in arrivo", "caixa de entrada", "caixa de correio", "входящие", "收件箱"}),
    "sent": frozenset({
        "sent", "sent items", "sent mail", "sent messages", "envoyés", "envoyes", "éléments envoyés",
        "elements envoyes", "messages envoyés", "courrier envoyé", "gesendet", "gesendete elemente",
        "gesendete objekte", "gesendete nachrichten", "enviados", "elementos enviados", "mensajes enviados",
        "correo enviado", "posta inviata", "inviati", "elementi inviati", "messaggi inviati", "itens enviados",
        "mensagens enviadas", "e-mails enviados", "отправленные", "отправленные элементы", "已发送邮件", "已发送",
        "已发送项目", "已发送的邮件"}),
    "drafts": frozenset({
        "drafts", "draft", "brouillons", "brouillon", "entwürfe", "entwurf", "borradores", "borrador", "bozze",
        "rascunhos", "rascunho", "черновики", "草稿", "草稿箱"}),
    "trash": frozenset({
        "trash", "deleted items", "deleted messages", "bin", "corbeille", "éléments supprimés",
        "elements supprimes", "messages supprimés", "gelöschte elemente", "gelöschte objekte", "papierkorb",
        "papelera", "elementos eliminados", "elementos borrados", "cestino", "posta eliminata",
        "elementi eliminati", "lixeira", "itens excluídos", "itens eliminados", "корзина", "удалённые",
        "удаленные", "удалённые элементы", "удаленные элементы", "已删除邮件", "已删除", "已删除项目",
        "已删除的邮件"}),
    "junk": frozenset({
        "junk", "junk e-mail", "junk email", "junk-e-mail", "junk-email", "spam", "courrier indésirable",
        "courrier indesirable", "indésirables", "pourriel", "pourriels", "unerwünschte e-mail",
        "correo no deseado", "no deseado", "posta indesiderata", "lixo eletrônico", "lixo eletrónico",
        "lixo", "нежелательная почта", "спам", "垃圾邮件", "垃圾电子邮件"}),
}
_ROLE_OF = {name: role for role, names in DEFAULT_NAMES.items() for name in names}


def default_role(path: list[str]) -> str | None:
    """Role suggested by the name of a folder, only for a top-level folder or one directly under the Inbox
    (clients put Sent, Drafts... there)."""
    if not path:
        return None
    if len(path) > 1 and not (len(path) == 2 and _ROLE_OF.get(path[0].strip().casefold()) == "inbox"):
        return None
    return _ROLE_OF.get(path[-1].strip().casefold())


@dataclass
class Mapped:
    """A source folder as the planner of the target must see it: `path` and `special` are those after mapping,
    `orig` is the real folder (its key is the one used for the items)."""
    path: list[str]
    special: str | None
    orig: object
    kind: str = "mail"
    count: int | None = None


def _parts(text: str) -> list[str]:
    return [p for p in str(text).replace("\\", "/").split("/") if p.strip()]


def _has_wildcard(text: str) -> bool:
    return any(c in text for c in "*?[")


@dataclass
class Rule:
    source: str
    target: str  # "@role" or a path "A/B"

    def role(self) -> str | None:
        return self.target[1:] if self.target.startswith("@") else None

    def match(self, path: list[str]) -> list[str] | None:
        """None: no match. Otherwise the path the folder gets (for "@role": the path itself)."""
        joined = "/".join(path).casefold()
        src = self.source.strip().strip("/").casefold()
        if _has_wildcard(src):
            return list(path) if fnmatch.fnmatchcase(joined, src) else None
        n = len(_parts(src))
        if joined == src or joined.startswith(src + "/"):
            if self.role():
                return list(path) if joined == src else None
            return _parts(self.target) + list(path[n:])
        return None


def check_rules(rules) -> list[dict]:
    """Validates the `folder_map` of a policy. Raises ValueError."""
    if rules in (None, ""):
        return []
    if not isinstance(rules, list):
        raise ValueError("folder_map must be a list of {source, target}")
    out = []
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("source"), str) or not r["source"].strip():
            raise ValueError("each folder_map rule needs a `source` pattern")
        t = r.get("target")
        if not isinstance(t, str) or not t.strip():
            raise ValueError("each folder_map rule needs a `target`")
        t = t.strip()
        if t.startswith("@") and t[1:] not in ROLES:
            raise ValueError(f"unknown role {t!r}: use one of " + ", ".join("@" + x for x in ROLES))
        if not t.startswith("@") and not _parts(t):
            raise ValueError("empty target path")
        out.append({"source": r["source"].strip(), "target": t})
    return out


@dataclass
class Mapper:
    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def of(cls, rules: list[dict] | None) -> "Mapper":
        return cls([Rule(r["source"], r["target"]) for r in (rules or [])])

    def resolve(self, folders: list) -> list[Mapped]:
        """Mapped view of the mail folders (built-in names only where the source flagged nothing)."""
        taken = {f.special for f in folders if getattr(f, "special", None)}
        out: list[Mapped] = []
        for f in folders:
            path, special = list(f.path), getattr(f, "special", None)
            for rule in self.rules:
                got = rule.match(path)
                if got is None:
                    continue
                if rule.role():
                    special = rule.role()
                else:
                    path = got
                    special = None
                break
            else:
                if special is None:
                    role = default_role(path)
                    if role and role not in taken:
                        special = role
                        taken.add(role)
            out.append(Mapped(path, special, f, getattr(f, "kind", "mail"), getattr(f, "count", None)))
        return out


def plan_mapped(plan_fn: Callable[[list], dict], folders: list, mapper: Mapper | None) -> dict:
    """Runs a target planner (`plan_fn(folders) -> {folder_key(f.path): plan}`) on the mapped folders and returns
    its answer keyed by the key of each ORIGINAL folder."""
    from .runner import folder_key
    mapped = (mapper or Mapper()).resolve(folders)
    raw = plan_fn(mapped)
    return {folder_key(m.orig.path): raw[folder_key(m.path)] for m in mapped}


def excluded(patterns: list[str] | None, path: list[str]) -> bool:
    """Is the folder (or one of its ancestors) in the exclusion list? Patterns are case-insensitive; with a
    wildcard they must match the whole path, otherwise they also cover the sub-folders."""
    if not patterns:
        return False
    joined = "/".join(path).casefold()
    for p in patterns:
        src = p.strip().strip("/").casefold()
        if not src:
            continue
        if _has_wildcard(src):
            if fnmatch.fnmatchcase(joined, src):
                return True
        elif joined == src or joined.startswith(src + "/"):
            return True
    return False
