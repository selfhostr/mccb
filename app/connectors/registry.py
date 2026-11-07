"""Registry and declarative catalogue of the connectors: `GET /api/connectors` is `catalogue()`, the UI renders
any kind from it, including kinds it has never heard of."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import ConnectorError, Source, Target

FIELD_TYPES = ("text", "password", "number", "boolean", "select", "url")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"            # one of FIELD_TYPES
    required: bool = False
    secret: bool = False          # encrypted at rest, never returned by the API
    default: Any = None
    options: tuple[str, ...] = ()  # type "select"
    help: str = ""

    def view(self) -> dict:
        out = {"name": self.name, "label": self.label, "type": self.type, "required": self.required,
               "secret": self.secret, "default": self.default}
        if self.options:
            out["options"] = list(self.options)
        if self.help:
            out["help"] = self.help
        return out


@dataclass(frozen=True)
class Role:
    """What a connector does as a source or as a target. factory(config, secrets, ctx) -> Source | Target."""
    factory: Callable[[dict, dict, dict], Any]
    items: tuple[str, ...]
    capabilities: frozenset[str] = frozenset()
    fields: tuple[Field, ...] = ()
    # text naming the mailbox behind a config (no secret, no network): same text = same migration state.
    identity: Callable[[dict], str] = field(default=lambda config: "")

    def view(self) -> dict:
        return {"items": list(self.items), "capabilities": sorted(self.capabilities),
                "fields": [f.view() for f in self.fields]}


@dataclass(frozen=True)
class ConnectorSpec:
    kind: str
    label: str
    description: str = ""
    source: Role | None = None
    target: Role | None = None

    def view(self) -> dict:
        return {"kind": self.kind, "label": self.label, "description": self.description,
                "source": self.source.view() if self.source else None,
                "target": self.target.view() if self.target else None}


_REGISTRY: dict[str, ConnectorSpec] = {}


def register(spec: ConnectorSpec) -> ConnectorSpec:
    _REGISTRY[spec.kind] = spec
    return spec


def unregister(kind: str) -> None:
    _REGISTRY.pop(kind, None)


def get(kind: str) -> ConnectorSpec | None:
    return _REGISTRY.get(kind)


def catalogue() -> list[dict]:
    return [_REGISTRY[k].view() for k in sorted(_REGISTRY)]


def role_of(kind: str, role: str) -> Role:
    spec = _REGISTRY.get(kind)
    r = getattr(spec, role, None) if spec else None
    if r is None:
        raise ConnectorError(f"unknown {role} connector '{kind}'", ConnectorError.CONFIG)
    return r


def make_source(kind: str, config: dict, secrets: dict, ctx: dict | None = None) -> Source:
    return role_of(kind, "source").factory(config, secrets, ctx or {})


def make_target(kind: str, config: dict, secrets: dict, ctx: dict | None = None) -> Target:
    return role_of(kind, "target").factory(config, secrets, ctx or {})


def namespace(kind: str, role: str, config: dict) -> str:
    """Name space of a migration: attaches `items` rows to this source and prefixes the import ids. Stable for
    a mailbox: changing the password or a timeout keeps the state, pointing at another mailbox starts afresh."""
    text = role_of(kind, role).identity(config)
    return "src-" + hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()[:16]


def normalize_config(role: Role, config: dict | None) -> dict:
    """Checks a config against the catalogue (unknown fields and secrets in clear are refused), applies the
    defaults and coerces the types. Raises ConnectorError (config)."""
    config = dict(config or {})
    known = {f.name: f for f in role.fields}
    for name in config:
        f = known.get(name)
        if f is None:
            raise ConnectorError(f"unknown field '{name}'", ConnectorError.CONFIG)
        if f.secret:
            raise ConnectorError(f"'{name}' is a secret: send it in `secrets`", ConnectorError.CONFIG)
    out: dict[str, Any] = {}
    for f in role.fields:
        if f.secret:
            continue
        v = config.get(f.name)
        if isinstance(v, str):
            v = v.strip()
        if v is None or v == "":
            v = f.default
        if v is None or v == "":
            if f.required:
                raise ConnectorError(f"'{f.name}' is required", ConnectorError.CONFIG)
            continue
        out[f.name] = _coerce(f, v)
    return out


def _coerce(f: Field, v: Any) -> Any:
    if f.type == "number":
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ConnectorError(f"'{f.name}' must be a number", ConnectorError.CONFIG) from None
    if f.type == "boolean":
        if isinstance(v, bool):
            return v
        raise ConnectorError(f"'{f.name}' must be true or false", ConnectorError.CONFIG)
    v = str(v)
    if f.type == "select" and f.options and v not in f.options:
        raise ConnectorError(f"'{f.name}' must be one of {', '.join(f.options)}", ConnectorError.CONFIG)
    return v


def merge_secrets(role: Role, current: dict, given: dict | None) -> dict:
    """Secrets to store after an update: `given` omitted keeps them all, {} clears them all, otherwise each field
    given replaces its value (an empty value removes it) and the others stay. Raises ConnectorError (config)."""
    names = {f.name: f for f in role.fields if f.secret}
    if given is None:
        out = {k: v for k, v in current.items() if k in names}
    elif not given:
        out = {}
    else:
        out = {k: v for k, v in current.items() if k in names}
        for k, v in given.items():
            if k not in names:
                raise ConnectorError(f"unknown secret '{k}'", ConnectorError.CONFIG)
            if v:
                out[k] = v
            else:
                out.pop(k, None)
    for f in names.values():
        if f.required and not out.get(f.name):
            raise ConnectorError(f"'{f.name}' is required", ConnectorError.CONFIG)
    return out
