"""The `mccb` command line (standard library only): arguments, upload with resume, watching a job."""
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "app" / "cli" / "mccb.py"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("mccb_cli", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    """Records the calls and answers from a script."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.url = "http://x"

    def call(self, method, path, body=None, data=None, headers=None):
        self.calls.append((method, path, body, data, headers))
        a = self.answers[(method, path.split("?")[0])] if (method, path.split("?")[0]) in self.answers else None
        if callable(a):
            return a()
        if isinstance(a, list):
            return a.pop(0) if len(a) > 1 else a[0]
        return a


def test_is_standard_library_only(cli):
    src = PATH.read_text()
    imports = {line.split()[1].split(".")[0] for line in src.splitlines() if line.startswith(("import ", "from "))}
    import sys
    assert imports <= set(sys.stdlib_module_names) | {"__future__"}


def test_parser(cli):
    p = cli.build_parser()
    a = p.parse_args(["run", "1", "2", "--name", "wave", "--delta", "--watch"])
    assert a.accounts == [1, 2] and a.name == "wave" and a.delta and a.watch
    assert p.parse_args(["upload", "x.pst", "--account", "5", "--wait"]).account == 5
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_no_token_is_refused(cli, monkeypatch, capsys):
    monkeypatch.delenv("MCCB_TOKEN", raising=False)
    assert cli.main(["status"]) == 4
    assert "no token" in capsys.readouterr().err


def test_upload_sends_only_the_missing_chunks_then_links(cli, tmp_path, monkeypatch):
    f = tmp_path / "jane.pst"
    f.write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    api = FakeApi({
        ("POST", "/api/uploads"): {"id": 7, "chunk_size": 4096, "received": [0], "chunks": 3, "status": "uploading"},
        ("PUT", "/api/uploads/7/chunks/1"): {"n": 1, "ok": True},
        ("PUT", "/api/uploads/7/chunks/2"): {"n": 2, "ok": True},
        ("POST", "/api/uploads/7/complete"): {"ok": True},
        ("GET", "/api/psts/7"): [{"status": "validating"}, {"status": "valid"}],
        ("PUT", "/api/accounts/12/pst"): {"id": 12},
    })
    args = cli.build_parser().parse_args(["upload", str(f), "--account", "12"])
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.cmd_upload(api, args) == 0
    puts = [c for c in api.calls if c[0] == "PUT" and "/chunks/" in c[1]]
    assert [c[1] for c in puts] == ["/api/uploads/7/chunks/1", "/api/uploads/7/chunks/2"]  # chunk 0 was there
    import hashlib
    data = f.read_bytes()
    assert puts[0][3] == data[4096:8192] and puts[0][4]["X-Chunk-Sha256"] == hashlib.sha256(data[4096:8192]).hexdigest()
    assert len(puts[1][3]) == 10240 - 8192
    first = api.calls[0]
    assert first[2]["filename"] == "jane.pst" and first[2]["size"] == 10240 and isinstance(first[2]["last_modified"], int)
    assert ("PUT", "/api/accounts/12/pst", {"pst_id": 7}, None, None) in api.calls


def test_upload_of_a_file_already_on_the_server_sends_nothing(cli, tmp_path):
    f = tmp_path / "a.pst"
    f.write_bytes(b"x" * 10)
    api = FakeApi({("POST", "/api/uploads"): {"id": 3, "chunk_size": 4096, "received": [0], "chunks": 1,
                                              "status": "valid"}})
    assert cli.cmd_upload(api, cli.build_parser().parse_args(["upload", str(f)])) == 0
    assert [c[0] for c in api.calls] == ["POST"]


def test_watch_returns_the_outcome_as_an_exit_code(cli):
    job = {"id": 5, "status": "running", "phase": "mail", "progress": {"mail": {"done": 1, "total": 4, "errors": 0}}}
    for final, code in (("done", 0), ("done_with_errors", 1), ("failed", 2), ("stopped", 3)):
        api = FakeApi({("GET", "/api/jobs/5"): [job, {**job, "status": final}]})
        assert cli.watch(api, 5, interval=0) == code


def test_progress_line_shows_the_pause_reason(cli):
    line = cli.progress_line({"id": 1, "status": "paused", "pause_reason": "window", "phase": "mail", "progress": {}})
    assert "paused (window)" in line


def test_run_with_watch_gives_the_worst_exit_code(cli):
    api = FakeApi({
        ("POST", "/api/runs"): lambda: [{"id": 1, "account_id": 10, "status": "queued"}],
        ("GET", "/api/jobs/1"): {"id": 1, "status": "done_with_errors", "phase": "verify", "progress": {}},
    })
    args = cli.build_parser().parse_args(["run", "10", "--name", "w", "--watch", "--interval", "0"])
    assert cli.cmd_run(api, args) == 1
    assert json.dumps(api.calls[0][2], sort_keys=True) == json.dumps(
        {"account_ids": [10], "mode": "full", "priority": 0, "name": "w"}, sort_keys=True)


def test_human_sizes(cli):
    assert cli.human(512) == "512 B" and cli.human(2048) == "2.0 KB" and cli.human(None) == "?"
