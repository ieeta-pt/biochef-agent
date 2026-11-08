"""The TES runner: a step as a task sent to a server that already exists.

SPEC.md, section 14.2. What the agent keeps is tested here against a server of
its own (tests/tes_support.py), through the real tes-step script, the real wrapper
the task runs and real Snakemake: the image by digest, a task with nothing of the
agent's in it, the arrival checked by SHA-256 before it is placed, the files given
back through the helper, and a killed run's task cancelled.

What the sandbox does is the server's, and it is not tested here: the runner says
`attested`, and the rule that reads that is tested in test_runner_guarantees.py.
"""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import pytest

import tes
from runner import ATTESTED, ENFORCED, NONE, PROVIDERS, TesRunner, get_runner
from tests.tes_support import TesServer

STEP = REPO_ROOT / "tes-step"
STEP_ROOT = REPO_ROOT / "tes-step-root"
DIGEST = "a" * 64
IMAGE = f"registry.example/biochef/tools@sha256:{DIGEST}"
TOKEN = "t0ken-for-the-server-only"


class _Workspace:
    def __init__(self, path):
        self.path = str(path)


def fake_sudo(tmp_path, monkeypatch, allowed=True):
    calls = tmp_path / "sudo-calls"
    script = tmp_path / "bin" / "sudo"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> {calls}\n'
                      f'echo --- >> {calls}\nexit {0 if allowed else 1}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    return calls


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in tes.ATTRIBUTES + ("BIOCHEF_TES_ISOLATION",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host with a TES server and everything the runner checks, as the user."""
    etc = tmp_path / "etc"
    etc.mkdir(mode=0o755)
    libexec = tmp_path / "libexec"
    libexec.mkdir(mode=0o755)
    helper = libexec / "tes-step-root"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o770)
    staging = Path(os.path.realpath(staging))
    conf = etc / "tes-step.conf"
    conf.write_text(f"# tes\nSTAGING_ROOT={staging}\n")
    conf.chmod(0o644)
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    calls = fake_sudo(tmp_path, monkeypatch)
    server = TesServer([str(staging)], token=TOKEN).start()

    class Host:
        pass

    found = Host()
    found.tmp, found.staging, found.conf, found.helper = tmp_path, staging, conf, helper
    found.token, found.server, found.sudo_calls, found.etc = token, server, calls, etc

    def make(**changes):
        arguments = {"url": server.url, "image": IMAGE, "auth": "bearer",
                     "token_file": str(token), "staging": str(staging),
                     "helper": str(helper), "isolation": "attested",
                     "step_conf": str(conf), "trusted_owner": os.getuid(), **changes}
        return TesRunner(**arguments)

    found.make = make
    yield found
    server.stop()


def run_step(host, monkeypatch, command, workspace=None, runner=None, **settings):
    """One step through tes.main, as tes-step runs it, in a workspace."""
    workspace = workspace or host.tmp / "runs" / "biochef-run-abc_1"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    runner = runner or host.make(**settings)
    out, err = io.StringIO(), io.StringIO()
    code = tes.main(["-c", command], environ=runner.environment(), out=out, err=err)
    return code, out.getvalue(), err.getvalue(), workspace


# --------------------------------------------------------------------------
# the runner is selectable, and says what it is


def test_tes_is_a_provider(host):
    assert PROVIDERS["tes"] is TesRunner
    assert host.make().name == "tes"


def test_it_describes_the_server_and_the_start_of_the_digest(host):
    described = host.make().describe()
    assert host.server.url in described and "stand-in" in described
    assert f"@sha256:{DIGEST[:12]}" in described and DIGEST not in described


def test_the_server_is_asked_who_it_is_at_start(host):
    host.make()
    assert any(h.get("Authorization") == f"Bearer {TOKEN}"
               for h in host.server.seen_headers)


def test_a_server_that_is_not_tes_stops_the_agent(host, monkeypatch):
    monkeypatch.setattr(tes.Client, "_call",
                        lambda self, *a, **k: {"type": {"artifact": "wes"}})
    with pytest.raises(ValueError, match="'wes' service, not a TES one"):
        host.make()


def test_a_server_that_does_not_answer_stops_the_agent(host):
    host.server.stop()
    with pytest.raises(ValueError, match="did not answer as one"):
        host.make()


def test_a_server_that_refuses_the_token_stops_the_agent(host, tmp_path):
    other = tmp_path / "other-token"
    other.write_text("a-different-token-1234\n")
    other.chmod(0o600)
    with pytest.raises(ValueError, match="401"):
        host.make(token_file=str(other))


# --------------------------------------------------------------------------
# what it guarantees


def test_the_network_and_filesystem_are_the_operators_word(host):
    assert host.make().guarantees == {"network": ATTESTED, "filesystem": ATTESTED,
                                      "environment": ENFORCED}
    assert host.make().isolates_network is False


def test_the_operator_may_say_the_server_does_not_isolate(host):
    assert host.make(isolation="none").guarantees == {
        "network": NONE, "filesystem": NONE, "environment": ENFORCED}


@pytest.mark.parametrize("value", ["", "yes", "enforced", "ATTESTED"])
def test_the_operators_statement_has_no_default(host, value):
    with pytest.raises(ValueError, match="BIOCHEF_TES_ISOLATION"):
        host.make(isolation=value)


def test_the_isolation_rule_refuses_it_unless_turned_off_on_purpose(host, monkeypatch):
    monkeypatch.setenv("BIOCHEF_RUNNER", "tes")
    for name, value in (("BIOCHEF_TES_URL", host.server.url),
                        ("BIOCHEF_TES_IMAGE", IMAGE), ("BIOCHEF_TES_AUTH", "bearer"),
                        ("BIOCHEF_TES_TOKEN_FILE", str(host.token)),
                        ("BIOCHEF_TES_STAGING", str(host.staging)),
                        ("BIOCHEF_TES_HELPER", str(host.helper)),
                        ("BIOCHEF_TES_ISOLATION", "attested")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("runner.TES_STEP_CONF", str(host.conf))
    monkeypatch.setattr("runner.TesRunner.__init__.__defaults__",
                        (None,) * 8 + (str(host.conf), os.getuid(), None, None))
    for unset in (None, "true"):
        if unset is None:
            monkeypatch.delenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION")
        else:
            monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", unset)
        with pytest.raises(ValueError, match="network isolation is attested"):
            get_runner("tes")
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    assert get_runner("tes").name == "tes"


# --------------------------------------------------------------------------
# the settings the runner refuses to start without


@pytest.mark.parametrize("image", [
    "", "debian:stable-slim", "docker://registry.example/t@sha256:" + DIGEST,
    "registry.example/t@sha256:" + "a" * 63, "registry.example/t@sha256:" + "A" * 64,
    "registry.example/t:1.2", "registry.example/t@sha512:" + "a" * 64,
    "registry.example/t@sha256:" + DIGEST + " --privileged",
])
def test_an_image_that_is_not_a_digest_is_refused(host, image):
    with pytest.raises(ValueError, match="by digest"):
        host.make(image=image)


@pytest.mark.parametrize("url,message", [
    ("", "not a URL"),
    ("ftp://tes.example", "http or https"),
    ("http://tes.example:8000", "plain http to another host"),
    ("https://user:pw@tes.example", "user or password"),
    ("https://tes.example/?a=b", "no query"),
    ("https://tes.example:99999", "bad port"),
    ("https://tes.example/a b", "not a URL"),
])
def test_a_url_that_would_leak_or_not_be_secure_is_refused(host, url, message):
    with pytest.raises(ValueError, match=message):
        host.make(url=url)


@pytest.mark.parametrize("url", ["https://tes.example", "https://tes.example/tes/",
                                 "http://127.0.0.1:8000", "http://localhost:8000",
                                 "http://[::1]:8000"])
def test_https_or_this_host_is_accepted(url):
    assert tes.check_url(url) == url.rstrip("/")


def test_auth_has_no_default(host):
    with pytest.raises(ValueError, match="BIOCHEF_TES_AUTH"):
        host.make(auth="")


def test_a_bearer_needs_its_token_file(host):
    with pytest.raises(ValueError, match="needs BIOCHEF_TES_TOKEN_FILE"):
        host.make(token_file="")


def test_a_token_others_can_read_is_refused(host):
    host.token.chmod(0o640)
    with pytest.raises(ValueError, match="readable by no one else"):
        host.make()


def test_a_token_that_is_a_link_is_refused(host):
    link = host.tmp / "link"
    link.symlink_to(host.token)
    with pytest.raises(ValueError, match="not a regular file"):
        host.make(token_file=str(link))


def test_a_token_with_auth_none_would_never_be_sent(host):
    with pytest.raises(ValueError, match="never be sent"):
        host.make(auth="none")


def test_an_empty_or_odd_token_is_refused(host):
    host.token.write_text("two words\n")
    with pytest.raises(ValueError, match="does not hold a token"):
        host.make()


def test_auth_none_sends_no_header(host):
    host.server.token = None
    host.make(auth="none", token_file="")
    assert all("Authorization" not in h for h in host.server.seen_headers)


# --------------------------------------------------------------------------
# basic authentication: a user, and a password in a file


PASSWORD = "a-synthetic-password-0451"


@pytest.fixture
def basic(host):
    """The server wants a user and a password, as Funnel does with BasicAuth."""
    host.server.token, host.server.basic = None, ("agent-user", PASSWORD)
    password = host.tmp / "password"
    password.write_text(PASSWORD + "\n")
    password.chmod(0o600)
    host.password = password

    def make(**changes):
        return host.make(**{"auth": "basic", "token_file": "", "user": "agent-user",
                            "password_file": str(password), **changes})

    host.make_basic = make
    return host


def test_basic_sends_the_user_and_the_password_in_a_header(basic):
    import base64
    basic.make_basic()
    wanted = "Basic " + base64.b64encode(f"agent-user:{PASSWORD}".encode()).decode()
    assert {h.get("Authorization") for h in basic.server.seen_headers} == {wanted}


def test_a_workflow_runs_through_a_server_that_wants_a_password(basic, monkeypatch):
    runner = basic.make_basic()
    code, out, err, workspace = run_step(basic, monkeypatch, "echo made > made.txt",
                                         runner=runner)
    assert code == 0, err
    assert (workspace / "made.txt").read_text() == "made\n"


def test_the_password_never_reaches_the_task_or_the_steps_environment(basic, monkeypatch):
    runner = basic.make_basic()
    run_step(basic, monkeypatch, "true", runner=runner)
    assert PASSWORD not in json.dumps(basic.server.posted)
    environment = runner.environment()
    assert environment["BIOCHEF_TES_PASSWORD_FILE"] == str(basic.password)
    assert environment["BIOCHEF_TES_USER"] == "agent-user"
    assert PASSWORD not in json.dumps(environment), "the path, never the password"


def test_a_wrong_password_stops_the_agent(basic):
    basic.password.write_text("another-synthetic-password\n")
    with pytest.raises(ValueError, match="401"):
        basic.make_basic()


def test_basic_needs_a_user_and_a_password_file(basic):
    with pytest.raises(ValueError, match="needs BIOCHEF_TES_USER"):
        basic.make_basic(user="")
    with pytest.raises(ValueError, match="needs BIOCHEF_TES_PASSWORD_FILE"):
        basic.make_basic(password_file="")


@pytest.mark.parametrize("user", ["a:b", "has space", "-leading", "x" * 129, "new\nline"])
def test_a_user_that_would_break_the_header_is_refused(basic, user):
    with pytest.raises(ValueError, match="BIOCHEF_TES_USER"):
        basic.make_basic(user=user)


def test_a_password_others_can_read_is_refused(basic):
    basic.password.chmod(0o640)
    with pytest.raises(ValueError, match="readable by no one else"):
        basic.make_basic()


def test_a_password_that_is_a_link_is_refused(basic):
    link = basic.tmp / "password-link"
    link.symlink_to(basic.password)
    with pytest.raises(ValueError, match="not a regular file"):
        basic.make_basic(password_file=str(link))


@pytest.mark.parametrize("text", ["short", "has a space in it", ""])
def test_an_empty_or_odd_password_is_refused(basic, text):
    basic.password.write_text(text + "\n")
    with pytest.raises(ValueError, match="does not hold a password"):
        basic.make_basic()


def test_the_settings_of_one_kind_of_auth_are_refused_with_another(basic):
    with pytest.raises(ValueError, match="so the token would never be sent"):
        basic.make_basic(token_file=str(basic.token))
    with pytest.raises(ValueError, match="would never be sent"):
        basic.make(auth="bearer", user="agent-user")
    with pytest.raises(ValueError, match="would never be sent"):
        basic.make(auth="bearer", password_file=str(basic.password))
    with pytest.raises(ValueError, match="would never be sent"):
        basic.make(auth="none", token_file="", user="agent-user")


def test_basic_over_plain_http_to_another_host_is_refused(basic):
    with pytest.raises(ValueError, match="plain http to another host"):
        basic.make_basic(url="http://tes.example:8000")


def test_the_staging_directory_must_exist(host):
    with pytest.raises(ValueError, match="existing directory"):
        host.make(staging=str(host.tmp / "nowhere"))


def test_the_staging_directory_may_not_be_a_link(host):
    link = host.tmp / "staging-link"
    link.symlink_to(host.staging)
    with pytest.raises(ValueError, match="named by no symlink"):
        host.make(staging=str(link))


def test_the_staging_directory_may_not_be_open_to_others(host):
    host.staging.chmod(0o775)
    with pytest.raises(ValueError, match="not be accessible to others"):
        host.make()


def test_the_staging_directory_must_be_the_files(host):
    other = host.tmp / "other"
    other.mkdir(mode=0o770)
    with pytest.raises(ValueError, match="is not STAGING_ROOT"):
        host.make(staging=str(other))


@pytest.mark.parametrize("helper", ["", "tes-step-root", "/no/such/helper"])
def test_the_helper_must_be_installed(host, helper):
    with pytest.raises(ValueError):
        host.make(helper=helper)


@pytest.mark.parametrize("target", ["conf", "etc", "helper"])
def test_a_writable_file_or_directory_stops_the_agent(host, target):
    path = {"conf": host.conf, "etc": host.etc, "helper": host.helper}[target]
    path.chmod(0o777 if target != "etc" else 0o777)
    with pytest.raises(ValueError, match="owned by root|file owned by root"):
        host.make()


def test_a_file_owned_by_someone_else_stops_the_agent(host):
    with pytest.raises(ValueError, match="owned by root"):
        host.make(trusted_owner=os.getuid() + 1)


@pytest.mark.parametrize("text,message", [
    ("", "missing or not valid"),
    ("STAGING_ROOT=/a\nSTAGING_ROOT=/b\n", "appears twice"),
    ("STAGING_ROOT=relative\n", "missing or not valid"),
    ("OTHER=1\n", "not a known KEY=value line"),
    ("nonsense\n", "not a known KEY=value line"),
])
def test_a_bad_file_stops_the_agent(host, text, message):
    host.conf.write_text(text)
    with pytest.raises(ValueError, match=message):
        host.make()


def test_without_the_sudo_rule_the_agent_does_not_start(host, monkeypatch):
    fake_sudo(host.tmp, monkeypatch, allowed=False)
    with pytest.raises(ValueError, match="without a password"):
        host.make()


def test_the_agent_never_runs_the_tes_runner_as_root(host, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="must not run as root"):
        host.make()


# --------------------------------------------------------------------------
# the task: image by digest, and nothing of the agent's in it


def test_the_task_is_built_from_the_command_and_the_image_alone():
    task = tes.build_task(IMAGE, "echo $(id) \"'; rm -rf /\"", "biochef-run-x_1", "s1",
                          "file:///stage/in", "file:///stage/out")
    assert task["name"] == "biochef-biochef-run-x_1-s1"
    executor, = task["executors"]
    assert executor["image"] == IMAGE
    assert executor["command"] == ["bash", "-c", tes.WRAPPER, "biochef-step",
                                   "echo $(id) \"'; rm -rf /\""]
    assert "rm -rf" not in tes.WRAPPER, "the command is an argument, never spliced in"
    assert set(executor) == {"image", "command"}, "no env, no stdin"
    assert task["inputs"] == [{"url": "file:///stage/in", "path": "/in",
                               "type": "DIRECTORY"}]
    assert task["outputs"] == [{"url": "file:///stage/out", "path": "/out",
                                "type": "DIRECTORY"}]
    assert set(task) == {"name", "description", "tags", "executors", "inputs",
                         "outputs"}


def test_no_secret_of_the_agents_reaches_the_task(host, monkeypatch):
    secrets = {"BIOCHEF_AUDIT_SECRET": "audit-secret-value-1",
               "AWS_SECRET_ACCESS_KEY": "aws-secret-value-2",
               "REGISTRY_PASSWORD": "registry-secret-value-3"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    code, out, err, _ = run_step(host, monkeypatch, "echo done")
    assert code == 0, err
    posted = json.dumps(host.server.posted)
    for value in (*secrets.values(), TOKEN, str(host.token)):
        assert value not in posted
    assert all("env" not in executor for task in host.server.posted
               for executor in task["executors"])
    assert any(h.get("Authorization") == f"Bearer {TOKEN}"
               for h in host.server.seen_headers), "the token went in a header"


def test_the_step_does_not_see_the_agents_environment(host, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_SECRET", "audit-secret-value-1")
    code, out, err, _ = run_step(host, monkeypatch,
                                 'if env | grep -q audit-secret; then echo LEAK; fi')
    assert code == 0, err
    assert "LEAK" not in out


def test_the_runner_gives_snakemake_what_the_step_script_needs(host):
    runner = host.make()
    environment = runner.environment()
    assert environment["BIOCHEF_TES_URL"] == host.server.url
    assert environment["BIOCHEF_TES_IMAGE"] == IMAGE
    assert environment["BIOCHEF_TES_TOKEN_FILE"] == str(host.token)
    assert TOKEN not in json.dumps(environment), "the path, never the token"


def test_the_preamble_is_valid_python_and_names_the_script(host):
    preamble = host.make().snakefile_preamble()
    compile(preamble.replace("shell.", "print."), "<preamble>", "exec")
    assert f'shell.executable("{STEP}")' in preamble
    assert 'shell.prefix("set -euo pipefail; ")' in preamble
    assert IMAGE not in preamble and TOKEN not in preamble


# --------------------------------------------------------------------------
# a step, end to end


def test_a_step_runs_and_its_files_come_back(host, monkeypatch):
    ws = host.tmp / "runs" / "biochef-run-abc_1"
    ws.mkdir(parents=True)
    (ws / "input.txt").write_text("alpha\n")
    code, out, err, _ = run_step(
        host, monkeypatch, "set -euo pipefail; tr a-z A-Z < input.txt > output.txt; "
        "echo to-stdout; echo to-stderr >&2", workspace=ws)
    assert code == 0, err
    assert (ws / "output.txt").read_text() == "ALPHA\n"
    assert "to-stdout" in out and "to-stderr" in err
    assert not (ws / tes.MANIFEST).exists()


def test_only_what_the_step_changed_comes_back(host, monkeypatch):
    ws = host.tmp / "runs" / "biochef-run-abc_1"
    ws.mkdir(parents=True)
    (ws / "big-input.bin").write_bytes(b"x" * 100_000)
    (ws / "edited.txt").write_text("one\n")
    seen = {}
    host.server.tamper = lambda out: seen.setdefault(
        "files", sorted(str(p.relative_to(out)) for p in Path(out).rglob("*")
                        if p.is_file()))
    code, out, err, _ = run_step(
        host, monkeypatch, "echo two >> edited.txt; mkdir -p sub/deeper; "
        "echo made > 'sub/deeper/new file.txt'", workspace=ws)
    assert code == 0, err
    assert seen["files"] == [tes.MANIFEST, "edited.txt", "sub/deeper/new file.txt"]
    assert (ws / "edited.txt").read_text() == "one\ntwo\n"
    assert (ws / "sub/deeper/new file.txt").read_text() == "made\n"
    assert (ws / "big-input.bin").stat().st_size == 100_000


def test_an_executable_stays_executable(host, monkeypatch):
    code, out, err, ws = run_step(
        host, monkeypatch, "printf '#!/bin/sh\\necho hi\\n' > tool; chmod +x tool")
    assert code == 0, err
    assert os.access(ws / "tool", os.X_OK)


def test_the_server_is_shown_the_staged_copy_and_nothing_else(host, monkeypatch):
    ws = host.tmp / "runs" / "biochef-run-abc_1"
    ws.mkdir(parents=True)
    (ws / "data.txt").write_text("data\n")
    (ws / "Snakefile").write_text("rule all: input: []\n")
    (ws / ".snakemake").mkdir()
    (ws / ".snakemake" / "metadata").write_text("secret state\n")
    shown = {}

    def look(task):
        root = Path(tes.urllib.parse.unquote(task["inputs"][0]["url"][len("file://"):]))
        shown["in"] = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        shown["root"] = str(root)

    host.server.on_task = look
    code, out, err, _ = run_step(host, monkeypatch, "true", workspace=ws)
    assert code == 0, err
    assert shown["in"] == ["data.txt"]
    assert shown["root"].startswith(str(host.staging / "biochef-run-abc_1"))


def test_a_link_in_the_workspace_is_not_handed_to_the_server(host, monkeypatch):
    ws = host.tmp / "runs" / "biochef-run-abc_1"
    ws.mkdir(parents=True)
    (ws / "passwd").symlink_to("/etc/passwd")
    code, out, err, _ = run_step(host, monkeypatch, "true", workspace=ws)
    assert code != 0 and "is not a regular file" in err
    assert host.server.posted == []
    assert not (host.staging / "biochef-run-abc_1" / "s").exists()


def test_a_failing_step_reports_its_own_exit_code_and_output(host, monkeypatch):
    code, out, err, ws = run_step(host, monkeypatch,
                                  "echo before; echo oops >&2; touch partial; exit 3")
    assert code == 3
    assert "before" in out and "oops" in err
    assert not (ws / "partial").exists(), "nothing of a failed step is placed"


def test_a_server_side_failure_is_a_failure(host, monkeypatch):
    monkeypatch.setattr(host.server, "_inside", lambda path: False)
    code, out, err, _ = run_step(host, monkeypatch, "true")
    assert code != 0 and "SYSTEM_ERROR" in err and "not in an allowed directory" in err


def test_the_step_directory_is_handed_back_and_removed(host, monkeypatch):
    code, out, err, ws = run_step(host, monkeypatch, "touch made")
    assert code == 0, err
    calls = [c for c in host.sudo_calls.read_text().split("---\n") if "finish" in c]
    assert calls, "the helper was never asked to give the files back"
    for call in calls:
        lines = call.split("\n")
        assert lines[:4] == ["-n", "--", str(host.helper), "finish"]
        assert Path(lines[4]).parent.parent == host.staging
    assert [p for p in (host.staging / ws.name).glob("*")] == []


def test_the_files_are_handed_back_before_they_are_read(host, monkeypatch):
    order = []
    original = tes.give_back
    monkeypatch.setattr(tes, "give_back", lambda s, d: (order.append("back"),
                                                         original(s, d))[1])
    original_verify = tes.verify_arrival
    monkeypatch.setattr(tes, "verify_arrival", lambda o: (order.append("verify"),
                                                          original_verify(o))[1])
    code, out, err, _ = run_step(host, monkeypatch, "touch made")
    assert code == 0, err
    assert order[:2] == ["back", "verify"]


# --------------------------------------------------------------------------
# the arrival is checked before it is placed


def corrupt(out):
    (Path(out) / "result.txt").write_text("not what the task wrote\n")


def add_file(out):
    (Path(out) / "extra.txt").write_text("nobody listed this\n")


def drop_file(out):
    (Path(out) / "result.txt").unlink()


def plant_link(out):
    (Path(out) / "result.txt").unlink()
    (Path(out) / "result.txt").symlink_to("/etc/passwd")


def no_manifest(out):
    (Path(out) / tes.MANIFEST).unlink()


def escape_manifest(out):
    digest = tes._sha256(str(Path(out) / "result.txt"))
    (Path(out) / tes.MANIFEST).write_text(f"{digest}  ./../escaped.txt\n")


def twice(out):
    manifest = Path(out) / tes.MANIFEST
    manifest.write_text(manifest.read_text() * 2)


def garbage(out):
    (Path(out) / tes.MANIFEST).write_text("this is not a manifest\n")


@pytest.mark.parametrize("tamper,message", [
    (corrupt, "different SHA-256"),
    (add_file, "not in the manifest"),
    (drop_file, "did not arrive"),
    (plant_link, "something other than a file"),
    (no_manifest, "no readable manifest"),
    (escape_manifest, "leaves the workspace"),
    (twice, "twice"),
    (garbage, "not a digest and a plain path"),
])
def test_an_arrival_that_does_not_match_is_never_placed(host, monkeypatch, tamper,
                                                        message):
    host.server.tamper = tamper
    code, out, err, ws = run_step(host, monkeypatch, "echo right > result.txt")
    assert code != 0 and message in err, err
    assert not (ws / "result.txt").exists()
    assert not (ws.parent / "escaped.txt").exists()
    assert not list(ws.iterdir()), "nothing at all was placed"


def test_a_special_file_in_the_arrival_is_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / tes.MANIFEST).write_text("")
    os.mkfifo(out / "pipe")
    with pytest.raises(tes.TesError, match="something other than a file"):
        tes.verify_arrival(str(out))


@pytest.mark.parametrize("name", ["Snakefile", ".snakemake/metadata", ".tes/x"])
def test_a_step_may_not_write_what_belongs_to_the_agent(host, monkeypatch, name):
    code, out, err, ws = run_step(
        host, monkeypatch, f"mkdir -p \"$(dirname '{name}')\"; echo evil > '{name}'")
    assert code != 0 and "belongs to the agent" in err
    assert not (ws / name).exists()


def test_nothing_is_placed_when_one_file_of_several_is_refused(host, monkeypatch):
    host.server.tamper = corrupt
    code, out, err, ws = run_step(host, monkeypatch,
                                  "echo a > first.txt; echo b > result.txt")
    assert code != 0
    assert not (ws / "first.txt").exists() and not (ws / "result.txt").exists()


def test_a_file_cannot_be_written_through_a_link_in_the_workspace(host, monkeypatch,
                                                                  tmp_path):
    ws = host.tmp / "runs" / "biochef-run-abc_1"
    ws.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code, out, err, _ = run_step(host, monkeypatch, "mkdir sub; echo x > sub/f",
                                 workspace=ws)
    assert code == 0, err
    (ws / "sub" / "f").unlink()
    (ws / "sub").rmdir()
    (ws / "sub").symlink_to(elsewhere)
    with pytest.raises(tes.TesError, match="through a link"):
        tes.apply_arrival(str(host.tmp), str(ws), ["sub/f"])
    assert list(elsewhere.iterdir()) == []


# --------------------------------------------------------------------------
# the token never leaves for another place


def test_a_redirect_is_not_followed_with_the_token(tmp_path):
    reached = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class Redirects(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location",
                             f"http://127.0.0.1:{target.server_address[1]}/v1/service-info")
            self.end_headers()

        def log_message(self, *args):
            pass

    source = HTTPServer(("127.0.0.1", 0), Redirects)
    threading.Thread(target=source.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    settings = tes.Settings(f"http://127.0.0.1:{source.server_address[1]}", IMAGE,
                            "bearer", str(token), None, str(tmp_path), "/h")
    try:
        with pytest.raises(tes.TesError, match="302"):
            tes.Client(settings).service_info()
    finally:
        source.shutdown()
        target.shutdown()
    assert reached == []


def test_the_proxy_settings_of_the_agent_are_not_used(host, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    host.make()


# --------------------------------------------------------------------------
# a whole workflow, through Snakemake


WORKFLOW = '''
rule all:
    input: "result.txt", "second.txt"

rule first:
    output: "result.txt"
    shell: "echo hello > result.txt"

rule second:
    input: "result.txt"
    output: "second.txt"
    shell: "tr a-z A-Z < result.txt > second.txt"
'''


def test_snakemake_runs_a_workflow_through_tasks(host, tmp_path):
    runner = host.make()
    ws = tmp_path / "runs" / "biochef-run-wf_1"
    ws.mkdir(parents=True)
    (ws / "Snakefile").write_text(runner.snakefile_preamble() + WORKFLOW)
    result = runner.run(_Workspace(ws), timeout_s=180)
    assert result.returncode == 0, result.stderr
    assert (ws / "result.txt").read_text() == "hello\n"
    assert (ws / "second.txt").read_text() == "HELLO\n"
    assert len(host.server.posted) == 2
    assert all(t["executors"][0]["image"] == IMAGE for t in host.server.posted)
    assert not (host.staging / ws.name).exists(), "staging is cleared at the end"


def test_a_failing_step_fails_the_workflow(host, tmp_path):
    runner = host.make()
    ws = tmp_path / "runs" / "biochef-run-wf_2"
    ws.mkdir(parents=True)
    (ws / "Snakefile").write_text(runner.snakefile_preamble() + '''
rule all:
    input: "out.txt"

rule bad:
    output: "out.txt"
    shell: "touch out.txt; exit 7"
''')
    result = runner.run(_Workspace(ws), timeout_s=180)
    assert result.returncode != 0
    assert not (ws / "out.txt").exists()


def test_a_run_that_times_out_has_its_task_cancelled(host, tmp_path):
    runner = host.make()
    ws = tmp_path / "runs" / "biochef-run-wf_3"
    ws.mkdir(parents=True)
    (ws / "Snakefile").write_text(runner.snakefile_preamble() + '''
rule all:
    input: "never.txt"

rule slow:
    output: "never.txt"
    shell: "sleep 120; touch never.txt"
''')
    started = time.monotonic()
    result = runner.run(_Workspace(ws), timeout_s=8)
    assert result.returncode != 0
    assert time.monotonic() - started < 60
    assert host.server.cancelled, "the task kept running on the server"
    assert not (host.staging / ws.name).exists()
    leftover = [r["state"] for r in host.server.tasks.values()]
    assert leftover and all(state in tes.TERMINAL for state in leftover)


def test_after_run_with_an_unreachable_server_does_not_hide_the_result(host, tmp_path):
    runner = host.make()
    host.server.stop()
    ws = tmp_path / "runs" / "biochef-run-wf_4"
    ws.mkdir(parents=True)
    (host.staging / ws.name).mkdir()
    runner.after_run(_Workspace(ws))
    assert not (host.staging / ws.name).exists()


def test_a_term_signal_cancels_the_task(host, tmp_path):
    runner = host.make()
    ws = tmp_path / "runs" / "biochef-run-wf_5"
    ws.mkdir(parents=True)
    process = subprocess.Popen([str(STEP), "-c", "sleep 120"], cwd=ws,
                               env=runner.environment(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if host.server.tasks and any(r["state"] == "RUNNING"
                                         for r in host.server.tasks.values()):
                break
            time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=60)
    finally:
        process.kill()
    assert process.returncode == 143, err
    assert host.server.cancelled


# --------------------------------------------------------------------------
# the script and the helper


def test_the_scripts_are_executable_and_parse():
    assert os.access(STEP, os.X_OK) and os.access(STEP_ROOT, os.X_OK)
    subprocess.run(["bash", "-n", str(STEP_ROOT)], check=True)
    compile(STEP.read_text(), "tes-step", "exec")


def test_the_step_script_refuses_what_it_was_not_asked_for(tmp_path):
    for argv in ([], ["-c"], ["-x", "true"], ["-c", "a", "b"]):
        done = subprocess.run([str(STEP), *argv], cwd=tmp_path, capture_output=True,
                              text=True, env={"PATH": os.environ["PATH"]})
        assert done.returncode == 2 and "expects: -c <command>" in done.stderr


def test_the_step_script_refuses_without_its_settings(tmp_path):
    done = subprocess.run([str(STEP), "-c", "true"], cwd=tmp_path, capture_output=True,
                          text=True, env={"PATH": os.environ["PATH"]})
    assert done.returncode == 2 and "BIOCHEF_TES_AUTH" in done.stderr


def test_the_helper_and_the_agent_read_the_same_file():
    import runner
    assert f"CONF={runner.TES_STEP_CONF}\n" in STEP_ROOT.read_text()


def test_the_helper_does_nothing_but_hand_back():
    script = STEP_ROOT.read_text()
    assert 'chown -R -P "$SUDO_UID:$SUDO_GID"' in script
    assert "chmod -R u+rwX" in script
    for forbidden in ("podman", "docker", "eval", "bash -c", '"$@"', "rm "):
        assert forbidden not in script.replace("# ", "#"), forbidden
    assert script.count("finish)") == 1 and "run)" not in script


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the refusal of a non-root caller")
def test_the_helper_refuses_anyone_but_root(tmp_path):
    done = subprocess.run([str(STEP_ROOT), "finish", str(tmp_path)],
                          capture_output=True, text=True)
    assert done.returncode == 2 and "must run as root" in done.stderr
