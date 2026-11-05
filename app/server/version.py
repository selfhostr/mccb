"""Single source of truth for the app version (app/VERSION)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _file_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _git_sha() -> str:
    sha = os.environ.get("GIT_SHA", "").strip()
    if sha:
        return sha
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(VERSION_FILE.parent),
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def get_version() -> str:
    v = os.environ.get("APP_VERSION", "").strip()
    if v:
        return v
    base = _file_version()
    sha = _git_sha()
    return f"{base}+{sha}" if sha else base
