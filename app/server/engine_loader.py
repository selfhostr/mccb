"""Loading of the ENGINE module (replaceable by a fake engine via PSTMIG_ENGINE)."""
from __future__ import annotations

import importlib

from .config import settings


def engine():
    return importlib.import_module(settings.engine_module)


def check_signature(path: str) -> bool:
    """PST signature "!BDN" (checked here AND by the engine)."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"!BDN":
                return False
    except OSError:
        return False
    fn = getattr(engine(), "check_pst_signature", None)
    return bool(fn(path)) if fn else True
