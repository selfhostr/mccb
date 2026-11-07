"""Text formats shared by the connectors: vCard 3.0 writing and reading, and the mapping between vCard and
JSContact cards (RFC 9553 for the model, RFC 9555 for the correspondence) used by the JMAP contacts of RFC 9610.

Only the properties people actually fill in are mapped (names, emails, phones, addresses, organizations, titles,
nicknames, notes, links, birthday and other anniversaries). Everything else is dropped and reported as a warning."""
from __future__ import annotations

import re
from typing import Iterable

# ------------------------------------------------------------------ vCard text
_ESC = str.maketrans({"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n", "\r": ""})


def esc(value: str) -> str:
    return (value or "").translate(_ESC)


def unesc(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def fold(line: str, limit: int = 75) -> str:
    """Folds a content line at `limit` octets (RFC 6350 section 3.2), never inside a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= limit:
        return line
    parts, cur, size, first = [], [], 0, True
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (limit if first else limit - 1):
            parts.append("".join(cur))
            cur, size, first = [], 0, False
        cur.append(ch)
        size += n
    parts.append("".join(cur))
    return "\r\n ".join(parts)


def join_lines(lines: Iterable[str]) -> str:
    return "".join(fold(x) + "\r\n" for x in lines)


def split_unescaped(value: str, sep: str) -> list[str]:
    out, cur, i = [], [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            cur.append(value[i:i + 2])
            i += 2
            continue
        if c == sep:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_line(line: str) -> tuple[str, dict[str, list[str]], str] | None:
    """(NAME, {PARAM: [values]}, raw value) of a content line, group prefix removed."""
    in_q, colon = False, -1
    for i, c in enumerate(line):
        if c == '"':
            in_q = not in_q
        elif c == ":" and not in_q:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    segs, cur, in_q = [], [], False
    for c in head:
        if c == '"':
            in_q = not in_q
        if c == ";" and not in_q:
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    segs.append("".join(cur))
    name = segs[0].split(".")[-1].upper()
    params: dict[str, list[str]] = {}
    for seg in segs[1:]:
        k, _, v = seg.partition("=")
        if not _:  # vCard 2.1 style bare type
            params.setdefault("TYPE", []).append(k.upper())
        else:
            params.setdefault(k.upper(), []).extend(x.strip('"') for x in v.split(","))
    return name, params, value


# ------------------------------------------------------------------ JSContact -> vCard
_CTX_TYPE = {"work": "work", "private": "home"}
_FEATURE_TYPE = {"mobile": "cell", "voice": "voice", "text": "text", "video": "video", "fax": "fax",
                 "pager": "pager", "textphone": "textphone", "main-number": "pref"}
_NAME_ORDER = ("surname", "given", "given2", "title", "credential")


def _types(obj: dict, table: dict, extra: Iterable[str] = ()) -> str:
    found: list[str] = []
    for ctx in (obj.get("contexts") or {}):
        if ctx in _CTX_TYPE:
            found.append(_CTX_TYPE[ctx])
    for t in extra:
        if t not in found:
            found.append(t)
    if obj.get("pref") == 1:
        found.append("pref")
    return (";TYPE=" + ",".join(found)) if found else ""


def _by_pref(items: dict | None) -> list[dict]:
    vals = [v for v in (items or {}).values() if isinstance(v, dict)]
    return sorted(vals, key=lambda v: v.get("pref") or 100)


def _name_text(name: dict | None) -> str:
    if not name:
        return ""
    if name.get("full"):
        return name["full"]
    comps = [c for c in name.get("components") or [] if isinstance(c, dict)]
    if name.get("isOrdered"):
        sep = name.get("defaultSeparator") or " "
        out, after_sep = "", True
        for c in comps:
            if c.get("kind") == "separator":
                out += c.get("value", "")
                after_sep = True
            else:
                out += ("" if after_sep or not out else sep) + c.get("value", "")
                after_sep = False
        return out.strip()
    return " ".join(c.get("value", "") for c in comps if c.get("kind") != "separator").strip()


def _address_parts(adr: dict) -> list[str]:
    """[pobox, extended, street, locality, region, postcode, country] of a JSContact Address."""
    by: dict[str, list[str]] = {}
    for c in adr.get("components") or []:
        if isinstance(c, dict) and c.get("kind") != "separator" and c.get("value"):
            by.setdefault(c.get("kind", ""), []).append(c["value"])
    j = lambda *kinds: " ".join(v for k in kinds for v in by.get(k, []))  # noqa: E731
    street = j("number", "name", "block", "direction", "landmark") or (adr.get("full") if not by else "")
    return [j("postOfficeBox"), j("room", "apartment", "floor", "building"), street,
            j("locality", "subdistrict", "district"), j("region"), j("postcode"), j("country")]


def _birth(card: dict) -> tuple[str | None, str | None]:
    for a in (card.get("anniversaries") or {}).values():
        if isinstance(a, dict) and a.get("kind") == "birth":
            d = a.get("date") or {}
            if d.get("@type") == "Timestamp" and d.get("utc"):
                return d["utc"][:10], None
            if d.get("year") and d.get("month") and d.get("day"):
                return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}", None
            return None, "birthday without a year not carried"
    return None, None


def jscontact_to_vcard(card: dict) -> tuple[str, list[str]] | None:
    """(vCard 3.0 text, warnings). None for a group card (nothing to put in an address book as a person)."""
    if card.get("kind") == "group":
        return None
    warns: list[str] = []
    name = card.get("name") or {}
    fn = _name_text(name)
    orgs = _by_pref(card.get("organizations"))
    emails = _by_pref(card.get("emails"))
    if not fn:
        fn = (orgs[0].get("name") if orgs else "") or (emails[0].get("address") if emails else "") or "Unnamed"
    comps: dict[str, str] = {}
    for c in name.get("components") or []:
        if isinstance(c, dict) and c.get("kind") in _NAME_ORDER:
            comps[c["kind"]] = (comps[c["kind"]] + " " if c["kind"] in comps else "") + c.get("value", "")
    uid = card.get("uid") or card.get("id") or ""
    lines = ["BEGIN:VCARD", "VERSION:3.0", "UID:" + esc(str(uid)), "FN:" + esc(fn),
             "N:" + ";".join(esc(comps.get(k, "")) for k in _NAME_ORDER)]
    for n in _by_pref(card.get("nicknames")):
        lines.append("NICKNAME:" + esc(n.get("name", "")))
    for o in orgs:
        lines.append("ORG:" + ";".join(esc(x) for x in [o.get("name", "")] + [u.get("name", "") for u in o.get("units") or []]))
    for t in _by_pref(card.get("titles")):
        lines.append(("ROLE:" if t.get("kind") == "role" else "TITLE:") + esc(t.get("name", "")))
    for e in emails:
        lines.append("EMAIL" + _types(e, {}, ()) + ":" + esc(e.get("address", "")))
    for p in _by_pref(card.get("phones")):
        feats = [_FEATURE_TYPE[f] for f in (p.get("features") or {}) if f in _FEATURE_TYPE]
        num = p.get("number", "")
        lines.append("TEL" + _types(p, {}, feats) + ":" + esc(num[4:] if num.lower().startswith("tel:") else num))
    for a in _by_pref(card.get("addresses")):
        lines.append("ADR" + _types(a, {}) + ":" + ";".join(esc(x) for x in _address_parts(a)))
    for lk in _by_pref(card.get("links")):
        if lk.get("uri"):
            lines.append("URL:" + lk["uri"])
    for s in _by_pref(card.get("onlineServices")):
        if s.get("uri") and ":" in s["uri"]:
            lines.append("URL:" + s["uri"])
    bday, w = _birth(card)
    if bday:
        lines.append("BDAY:" + bday)
    if w:
        warns.append(w)
    notes = [n.get("note", "") for n in (card.get("notes") or {}).values() if isinstance(n, dict) and n.get("note")]
    if notes:
        lines.append("NOTE:" + esc("\n\n".join(notes)))
    if card.get("keywords"):
        lines.append("CATEGORIES:" + ",".join(esc(k) for k in card["keywords"]))
    if card.get("updated"):
        lines.append("REV:" + card["updated"])
    if card.get("media"):
        warns.append("photo not carried")
    lines.append("END:VCARD")
    return join_lines(lines), warns


# ------------------------------------------------------------------ vCard -> JSContact
_TYPE_CTX = {"work": "work", "home": "private"}
_TYPE_FEATURE = {v: k for k, v in _FEATURE_TYPE.items() if k != "main-number"}


def _ctx_of(params: dict) -> tuple[dict, dict, int | None]:
    types = [t.lower() for t in params.get("TYPE", [])]
    ctx = {_TYPE_CTX[t]: True for t in types if t in _TYPE_CTX}
    feats = {_TYPE_FEATURE[t]: True for t in types if t in _TYPE_FEATURE}
    return ctx, feats, (1 if "pref" in types else None)


def vcard_to_jscontact(text: str) -> tuple[dict, list[str]]:
    """(JSContact card, warnings) from the first vCard of `text`. Raises ValueError when there is none."""
    props = []
    inside = False
    for line in unfold(text):
        parsed = parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.upper() == "VCARD":
            inside, props = True, []
        elif name == "END" and value.upper() == "VCARD":
            break
        elif inside:
            props.append((name, params, value))
    if not inside:
        raise ValueError("no vCard found")
    card: dict = {"@type": "Card", "version": "1.0", "kind": "individual"}
    counters: dict[str, int] = {}

    def add(prop: str, obj: dict) -> None:
        counters[prop] = counters.get(prop, 0) + 1
        card.setdefault(prop, {})[f"{prop[0]}{counters[prop]}"] = obj

    fn = ""
    warns: set[str] = set()
    for name, params, value in props:
        ctx, feats, pref = _ctx_of(params)
        extra = {"contexts": ctx} if ctx else {}
        if pref:
            extra["pref"] = pref
        if name == "UID":
            card["uid"] = unesc(value)
        elif name == "FN":
            fn = unesc(value)
        elif name == "N":
            parts = [unesc(x) for x in split_unescaped(value, ";")] + [""] * 5
            comps = []
            for kind, v in zip(("surname", "given", "given2", "title", "credential"), parts[:5]):
                if v:
                    comps.append({"kind": kind, "value": v})
            order = {"title": 0, "given": 1, "given2": 2, "surname": 3, "credential": 4}
            comps.sort(key=lambda c: order[c["kind"]])
            if comps:
                card["name"] = {"components": comps, "isOrdered": True}
        elif name == "NICKNAME":
            for n in split_unescaped(value, ","):
                add("nicknames", {"name": unesc(n)})
        elif name == "ORG":
            parts = [unesc(x) for x in split_unescaped(value, ";") if x]
            if parts:
                org = {"name": parts[0]}
                if len(parts) > 1:
                    org["units"] = [{"name": u} for u in parts[1:]]
                add("organizations", org)
        elif name in ("TITLE", "ROLE"):
            add("titles", {"name": unesc(value), "kind": "title" if name == "TITLE" else "role"})
        elif name == "EMAIL":
            add("emails", {"address": unesc(value), **extra})
        elif name == "TEL":
            v = unesc(value)
            obj = {"number": v, **extra}
            if feats:
                obj["features"] = feats
            add("phones", obj)
        elif name == "ADR":
            p = [unesc(x) for x in split_unescaped(value, ";")] + [""] * 7
            comps = []
            for kind, v in zip(("postOfficeBox", "apartment", "name", "locality", "region", "postcode", "country"), p[:7]):
                if v:
                    comps.append({"kind": kind, "value": v})
            if comps:
                add("addresses", {"components": comps, "isOrdered": False, **extra})
        elif name == "URL":
            add("links", {"uri": unesc(value), **({"pref": 1} if pref else {})})
        elif name == "NOTE":
            add("notes", {"note": unesc(value)})
        elif name == "BDAY":
            m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", value.strip())
            if m:
                add("anniversaries", {"kind": "birth", "date": {"year": int(m.group(1)), "month": int(m.group(2)),
                                                                 "day": int(m.group(3))}})
        elif name == "CATEGORIES":
            card.setdefault("keywords", {}).update({unesc(k): True for k in split_unescaped(value, ",") if k})
        elif name == "REV":
            m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})T?(\d{2}):?(\d{2}):?(\d{2})", value.strip())
            if m:
                card["updated"] = "%s-%s-%sT%s:%s:%sZ" % m.groups()
        elif name in ("PHOTO", "LOGO", "SOUND", "KEY"):
            warns.add(f"{name.lower()} not carried")
    if fn:
        card.setdefault("name", {})["full"] = fn
        card["name"].setdefault("isOrdered", False)
        if "components" not in card["name"]:
            card["name"]["components"] = [{"kind": "given", "value": fn}]
    return card, sorted(warns)
