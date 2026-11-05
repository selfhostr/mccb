"""Fixtures e2e (voir helpers.py). Ignorés sans les services de docker-compose.test.yml."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.helpers import ENABLED, Stack  # noqa: E402

if not ENABLED:
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(scope="module")
def stack_factory(tmp_path_factory):
    made: list[Stack] = []

    def make(**env) -> Stack:
        s = Stack(tmp_path_factory.mktemp("stack"), **env).start()
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


