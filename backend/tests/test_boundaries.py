import json
import socket
import sys
import threading
import time
from pathlib import Path
import pytest
from pebble.policy import Workspace, PolicyError
from pebble.sources import fetch_url, public_addresses, TextHTML
from pebble.app import App, TOOLS
from pebble.testing import run_tests


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "maths.py").write_text("def clamp(x, lo, hi):\n    return x\n")
    return root


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "data", tmp_path / "resources", lambda *args: None)
    yield a
    a.close()


@pytest.mark.parametrize(
    "name",
    [
        "../secret.txt",
        "/etc/passwd",
        ".env",
        ".env.production",
        ".ssh/id_rsa",
        "key.pem",
        "folder/token.key",
        ".git/config",
        "models/weights.gguf",
    ],
)
def test_sensitive_paths_rejected(project, name):
    with pytest.raises((PolicyError, FileNotFoundError)):
        Workspace(project).read(name)


def test_symlink_escape_and_parent_blocked(project, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (project / "linked.py").symlink_to(secret)
    (project / "outside").symlink_to(tmp_path, target_is_directory=True)
    for name in ("linked.py", "outside/secret.txt"):
        with pytest.raises(PolicyError):
            Workspace(project).read(name)
    assert "linked.py" not in Workspace(project).files()


def test_stage_apply_stale_and_undo(app, project):
    c = app.new_chat(str(project))
    ws = Workspace(project)
    before = (project / "maths.py").read_text()
    data = ws.draft(
        "maths.py", before.replace("return x", "return max(lo, min(hi, x))")
    )
    aid = app.propose(c["id"], "edit", data)["action_id"]
    assert (project / "maths.py").read_text() == before
    assert app.approve(c["id"], aid)["applied"] == "maths.py"
    with pytest.raises(PolicyError):
        app.approve(c["id"], aid)
    app.undo(c["id"], aid)
    assert (project / "maths.py").read_text() == before
    aid = app.propose(c["id"], "edit", ws.draft("maths.py", "changed"))["action_id"]
    (project / "maths.py").write_text("user edited meanwhile")
    with pytest.raises(PolicyError):
        app.approve(c["id"], aid)
    assert (project / "maths.py").read_text() == "user edited meanwhile"
    assert app.active is None


def test_forged_and_cross_chat_approvals_rejected(app, project):
    c = app.new_chat(str(project))
    other = app.new_chat()
    aid = app.propose(c["id"], "edit", Workspace(project).draft("new.py", "print(1)"))[
        "action_id"
    ]
    for cid, action in [(c["id"], "invented"), (other["id"], aid)]:
        with pytest.raises(PolicyError):
            app.approve(cid, action)
    assert not (project / "new.py").exists()
    app.reject(c["id"], aid)
    with pytest.raises(PolicyError):
        app.approve(c["id"], aid)


def test_symlink_added_after_draft_rejected(app, project, tmp_path):
    c = app.new_chat(str(project))
    data = Workspace(project).draft("new.py", "bad")
    aid = app.propose(c["id"], "edit", data)["action_id"]
    victim = tmp_path / "victim.py"
    victim.write_text("safe")
    (project / "new.py").symlink_to(victim)
    with pytest.raises(PolicyError):
        app.approve(c["id"], aid)
    assert victim.read_text() == "safe"


@pytest.mark.parametrize(
    "name,args",
    [
        ("Bash", {"command": "rm -rf ~"}),
        ("Read", {"file_path": "maths.py", "approve": True}),
        ("Read", {"file_path": ["maths.py"]}),
        ("OpenApp", {"name": "Terminal", "approved": True}),
        ("Read", {"file_path": "maths.py", "offset": True}),
        ("RunTests", {"recipe": "arbitrary"}),
    ],
)
def test_model_cannot_invent_authority(name, args):
    with pytest.raises(PolicyError):
        App.validate(name, args, TOOLS)


def test_external_source_is_not_user_authorization(app, project):
    c = app.new_chat(str(project))
    # Even an ALLOW from the reliability engine cannot override the policy broker.
    app.guard.handle = lambda *a, **kw: {"action": "allow"}
    with pytest.raises(PolicyError):
        app.execute(
            "Write",
            {"file_path": "../exfiltrate.py", "content": "anything"},
            Workspace(project),
            c["id"],
        )
    assert not (project.parent / "exfiltrate.py").exists()


def test_private_dns_and_mixed_dns_blocked(monkeypatch):
    for ips in [
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.2",),
        ("::1",),
        ("93.184.216.34", "192.168.1.1"),
    ]:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips
            ],
        )
        with pytest.raises(ValueError):
            public_addresses("example.test")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com:8080",
        "file:///etc/passwd",
    ],
)
def test_unsafe_web_source_syntax(url):
    with pytest.raises(ValueError):
        fetch_url(url)


def test_html_scripts_removed():
    parser = TextHTML()
    parser.feed("<h1>Useful</h1><script>steal()</script><style>evil</style><p>Data</p>")
    text = "".join(parser.parts)
    assert (
        "Useful" in text
        and "Data" in text
        and "steal" not in text
        and "evil" not in text
    )


def test_missing_workspace_task_releases_active(app, project):
    c = app.new_chat(str(project))
    (project / "maths.py").unlink()
    project.rmdir()
    app.send(c["id"], "explain this")
    deadline = time.monotonic() + 3
    while app.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app.active is None


def test_guard_indexes_only_scoped_mirror(app, project, tmp_path):
    (project / ".env").write_text("do not index")
    (project / "outside.py").symlink_to(tmp_path / "secret.py")
    result = app.guard.handle("session_start", project, "test-scope")
    assert result["action"] == "allow"
    mirror = app.guard.roots[(str(project), "test-scope")]
    assert (mirror / "maths.py").exists()
    assert not (mirror / ".env").exists() and not (mirror / "outside.py").exists()
    indexed = app.guard.guard.workspace(mirror)
    assert indexed.index.ready(timeout=5)
    assert indexed.root == mirror.resolve()
    assert indexed.store.identity.root == str(mirror.resolve())
    assert set(indexed.index.files) == {'maths.py'}


def test_guard_mirror_inside_git_checkout_never_discovers_foreign_files(tmp_path, monkeypatch):
    import subprocess
    from pebble.guard import GuardAdapter
    from agentguard.core import engine, store
    from agentguard.repo import scanner, index
    outer = tmp_path / 'outer'
    outer.mkdir()
    subprocess.run(['/usr/bin/git', '-c', 'init.templateDir=', 'init', '-q', str(outer)],
                   env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)}, check=True, capture_output=True)
    (outer / 'foreign.py').write_text('private_foreign_source = 1\n')
    selected = outer / 'selected'
    selected.mkdir()
    (selected / 'calc.py').write_text('def add(a, b):\n    return a+b\n')
    (selected / 'test_calc.py').write_text('from calc import add\n')
    guard = GuardAdapter(outer / 'host-data')
    def forbidden(*args, **kwargs):
        pytest.fail('A broker-scoped mirror must never discover an enclosing Git repository.')
    monkeypatch.setattr(engine, 'repository_root', forbidden)
    monkeypatch.setattr(store, 'repository_root', forbidden)
    monkeypatch.setattr(store, '_git_remote', forbidden)
    monkeypatch.setattr(scanner, '_git_listed_files', forbidden)
    monkeypatch.setattr(index.GitCache, 'get', forbidden)
    try:
        assert guard.begin(selected, 'scope', 'Update calc.py')['state'] == 'started'
        mirror = guard.roots[(str(selected), 'scope')]
        scoped = guard.guard.workspace(mirror)
        assert scoped.index.ready(timeout=5)
        assert scoped.root == mirror.resolve() and scoped.index.root == mirror.resolve()
        assert scoped.store.identity.root == str(mirror.resolve())
        assert scoped.store.identity.git_remote == '' and scoped.index.git.is_repo is False
        assert set(scoped.index.files) == {'calc.py', 'test_calc.py'}
    finally:
        guard.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox")
def test_isolated_tests_deny_network_home_and_original_writes(project, tmp_path):
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("private")
    test = f"""import unittest, socket\nfrom pathlib import Path\nclass Isolation(unittest.TestCase):\n def test_no_external_access(self):\n  with self.assertRaises(PermissionError): Path({str(sentinel)!r}).read_text()\n  with self.assertRaises(PermissionError): Path({str(project / "maths.py")!r}).write_text('corrupt')\n  with self.assertRaises(OSError): socket.create_connection(('1.1.1.1',443),timeout=1)\n"""
    (project / "test_isolation.py").write_text(test)
    result = run_tests(
        {"root": str(project), "recipe": "python-unittest", "drafts": []},
        threading.Event(),
    )
    assert result["exit_code"] == 0, result["output"]
    assert "OK" in result["output"]
    assert (project / "maths.py").read_text().startswith("def clamp")


def test_drafts_are_used_only_in_snapshot(project):
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox")
    (project / "test_maths.py").write_text(
        "import unittest\nfrom maths import clamp\nclass TestMaths(unittest.TestCase):\n def test_clamp(self): self.assertEqual(clamp(9,0,5),5)\n"
    )
    draft = Workspace(project).draft(
        "maths.py", "def clamp(x,lo,hi):\n    return max(lo,min(hi,x))\n"
    )
    result = run_tests(
        {"root": str(project), "recipe": "python-unittest", "drafts": [draft]},
        threading.Event(),
    )
    assert result["exit_code"] == 0, result["output"]
    assert (project / "maths.py").read_text().endswith("return x\n")
