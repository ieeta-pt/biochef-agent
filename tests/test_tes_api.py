"""The TES API: a listener of its own that refuses first and runs second.

SPEC.md, section 14.4. A TRE's own workflow engines send it tasks. What is tested
is what it refuses (no token, an image it was not told of, a URL that is not
file://, a directory it was not given, anything of the node's own data), what it
runs through (the node's runner, with that runner's guarantees, in a queue and
slots of its own) and what it records (audit events with source "tes").
"""

import json
import os
import re
import subprocess
import sys
import time
import types
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
from fastapi.testclient import TestClient

import audit
from runner import ATTESTED, ENFORCED, NONE, ApptainerRunner, PodmanRunner, SubprocessRunner
from tesapi import config as settings
from tesapi import tasks
from tesapi.app import Throttle, create_app
from tesapi.policy import Policy, Refused

DIGEST = "b" * 64
IMAGE = f"registry.example/biochef/tools@sha256:{DIGEST}"
OTHER_IMAGE = f"registry.example/biochef/other@sha256:{'c' * 64}"
TOKEN_A, TOKEN_B = "token-for-a-1234567890", "token-for-b-1234567890"


class RecordingRunner(SubprocessRunner):
    """Runs on the host, and remembers which image it was asked for."""

    def __init__(self, used=None, network=NONE):
        self.used = [] if used is None else used
        self.network = network

    def with_image(self, image):
        self.used.append(image)
        return RecordingRunner(self.used, self.network)

    @property
    def guarantees(self):
        return {"network": self.network, "filesystem": NONE, "environment": NONE}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A node's directories, a token file, an audit trail, and a settings mapping."""
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    data = tmp_path / "datasets"
    data.mkdir()
    (data / "cohort").mkdir()
    (data / "cohort" / "patients.txt").write_text("synthetic patient 1\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    secret = tmp_path / "secrets"
    secret.mkdir()
    tokens = secret / "tokens"
    tokens.write_text(
        f"alice sha256:{settings.hash_token(TOKEN_A)}\n"
        f"# a second caller\nbob sha256:{settings.hash_token(TOKEN_B)}\n")
    tokens.chmod(0o600)
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(trail))
    values = {
        "BIOCHEF_AUDIT_LOG": str(trail), "BIOCHEF_TESAPI_TOKENS": str(tokens),
        "BIOCHEF_TESAPI_IMAGES": IMAGE, "BIOCHEF_TESAPI_DIRS": str(exchange),
        "BIOCHEF_LOCAL_ROOT": str(data), "BIOCHEF_RUN_ROOT": str(runs),
        "BIOCHEF_TESAPI_TIMEOUT": "120", "BIOCHEF_DB_URL": f"sqlite:///{tmp_path}/agent.db",
    }

    class World:
        pass

    found = World()
    found.tmp, found.exchange, found.data, found.runs = tmp_path, exchange, data, runs
    found.tokens, found.trail, found.values = tokens, trail, values
    found.config = lambda **changes: settings.load({**values, **changes})

    def events(name=None):
        if not trail.exists():
            return []
        lines = [json.loads(l) for l in trail.read_text().splitlines()]
        return [e for e in lines if name is None or e["event"] == name]

    found.events = events
    return found


@pytest.fixture
def api(world):
    runner = RecordingRunner()
    app = create_app(world.config(), runner)
    with TestClient(app) as client:
        world.client, world.runner, world.app = client, runner, app
        yield world


def alice(client=None):
    return {"Authorization": f"Bearer {TOKEN_A}"}


def bob():
    return {"Authorization": f"Bearer {TOKEN_B}"}


def task(world, command, **changes):
    body = {"name": "t", "executors": [{"image": IMAGE, "command": command}],
            "inputs": [], "outputs": []}
    body.update(changes)
    return body


def url(path):
    return "file://" + str(path)


def wait(world, task_id, headers=None, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        view = world.client.get(f"/v1/tasks/{task_id}?view=FULL",
                                headers=headers or alice()).json()
        if view["state"] in tasks.TERMINAL:
            return view
        time.sleep(0.2)
    raise AssertionError(f"task still {view['state']}")


# --------------------------------------------------------------------------
# the listener refuses to start without what decides what a task may touch


@pytest.mark.parametrize("name", ["BIOCHEF_AUDIT_LOG", "BIOCHEF_TESAPI_TOKENS",
                                  "BIOCHEF_TESAPI_IMAGES", "BIOCHEF_TESAPI_DIRS"])
def test_nothing_that_decides_what_a_task_may_touch_has_a_default(world, name):
    with pytest.raises(settings.ConfigError, match=name):
        world.config(**{name: ""})


def test_it_loads(world):
    config = world.config()
    assert config.images == (IMAGE,) and config.dirs == (str(world.exchange.resolve()),)
    assert (config.host, config.port, config.max_concurrent) == ("127.0.0.1", 8082, 1)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "tes.internal"])
def test_off_this_host_it_needs_tls(world, host):
    with pytest.raises(settings.ConfigError, match="TLS"):
        world.config(BIOCHEF_TESAPI_HOST=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.9.9.9"])
def test_on_this_host_it_does_not(world, host):
    assert world.config(BIOCHEF_TESAPI_HOST=host).host == host


def test_tls_names_both_files_or_neither(world):
    cert = world.tmp / "cert.pem"
    cert.write_text("x")
    with pytest.raises(settings.ConfigError, match="go together"):
        world.config(BIOCHEF_TESAPI_TLS_CERT=str(cert))
    key = world.tmp / "key.pem"
    key.write_text("x")
    assert world.config(BIOCHEF_TESAPI_HOST="0.0.0.0", BIOCHEF_TESAPI_TLS_CERT=str(cert),
                        BIOCHEF_TESAPI_TLS_KEY=str(key)).tls_cert == str(cert)


def test_a_token_file_others_can_read_is_refused(world):
    world.tokens.chmod(0o640)
    with pytest.raises(settings.ConfigError, match="readable by no one else"):
        world.config()


@pytest.mark.parametrize("text,message", [
    ("", "lists no token"),
    ("alice plaintext-token\n", "expected `name sha256"),
    (f"alice sha256:{'a' * 64}\nalice sha256:{'b' * 64}\n", "appears twice"),
    (f"bad name sha256:{'a' * 64}\n", "expected"),
])
def test_a_token_file_that_is_wrong_stops_it(world, text, message):
    world.tokens.write_text(text)
    with pytest.raises(settings.ConfigError, match=message):
        world.config()


@pytest.mark.parametrize("image", [
    "debian:stable-slim", "docker://r/t@sha256:" + DIGEST, "r/t@sha256:" + "b" * 63,
    "r/t:1.2.3",
])
def test_an_image_that_is_not_a_digest_stops_it(world, image):
    with pytest.raises(settings.ConfigError, match="by digest"):
        world.config(BIOCHEF_TESAPI_IMAGES=f"{IMAGE},{image}")


def test_the_images_are_a_list(world):
    assert world.config(BIOCHEF_TESAPI_IMAGES=f"{IMAGE}, {OTHER_IMAGE}").images == (
        tuple(sorted((IMAGE, OTHER_IMAGE))))


@pytest.mark.parametrize("path", ["relative/dir", "/", "/no/such/dir"])
def test_a_directory_that_is_not_a_plain_directory_stops_it(world, path):
    with pytest.raises(settings.ConfigError):
        world.config(BIOCHEF_TESAPI_DIRS=path)


def test_a_directory_named_through_a_symlink_stops_it(world):
    link = world.tmp / "link"
    link.symlink_to(world.exchange)
    with pytest.raises(settings.ConfigError, match="symlink"):
        world.config(BIOCHEF_TESAPI_DIRS=str(link))


@pytest.mark.parametrize("name,where", [
    ("BIOCHEF_LOCAL_ROOT", "datasets"),
    ("BIOCHEF_LOCAL_ROOT", "datasets/cohort"),
    ("BIOCHEF_RUN_ROOT", "runs"),
    ("BIOCHEF_AUDIT_LOG", "."),
    ("BIOCHEF_TESAPI_TOKENS", "secrets"),
    ("BIOCHEF_DB_URL", "."),
])
def test_no_directory_may_contain_or_lie_in_the_nodes_own_data(world, name, where):
    allowed = (world.tmp / where).resolve()
    with pytest.raises(settings.ConfigError, match="overlaps"):
        world.config(BIOCHEF_TESAPI_DIRS=str(allowed))


def test_a_directory_beside_the_datasets_is_fine(world):
    side = world.tmp / "datasets-public"
    side.mkdir()
    assert world.config(BIOCHEF_TESAPI_DIRS=str(side.resolve())).dirs == (
        str(side.resolve()),)


def test_the_token_command_makes_a_line_the_file_accepts(world, capsys):
    from tesapi.__main__ import new_token
    assert new_token("carol") == 0
    out = capsys.readouterr().out
    token = re.search(r"^  (\S{20,})$", out, re.M).group(1)
    line = re.search(r"^  (carol sha256:[0-9a-f]{64})$", out, re.M).group(1)
    world.tokens.write_text(line + "\n")
    assert dict(world.config().tokens)["carol"] == settings.hash_token(token)
    assert new_token("not a name") == 2


# --------------------------------------------------------------------------
# authentication is mandatory, on every route


ROUTES = [("get", "/v1/service-info"), ("get", "/v1/tasks"),
          ("post", "/v1/tasks"), ("get", "/v1/tasks/x"), ("post", "/v1/tasks/x:cancel")]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-123456"},
                                     {"Authorization": "Bearer "},
                                     {"Authorization": f"Basic {TOKEN_A}"},
                                     {"Authorization": TOKEN_A}])
def test_no_route_answers_without_a_token(api, method, path, headers):
    answer = getattr(api.client, method)(path, headers=headers)
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_there_are_no_open_pages(api, path):
    assert api.client.get(path, headers=alice()).status_code == 404
    assert api.client.get(path).status_code in (401, 404)


def test_the_token_works_and_service_info_says_what_it_is(api):
    info = api.client.get("/v1/service-info", headers=alice()).json()
    assert info["type"] == {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
    assert info["storage"] == [f"file://{api.exchange.resolve()}"]


def test_refused_logins_are_recorded_and_batched(api):
    for _ in range(5):
        api.client.get("/v1/tasks")
    refused = api.events("tes.auth.refused")
    assert len(refused) == 1 and refused[0]["source"] == "tes"
    assert refused[0]["status"] == 401 and "token" not in json.dumps(refused[0]).lower().replace(
        "no valid token", "")


def test_the_throttle_counts_what_it_did_not_write():
    clock = [0.0]
    throttle = Throttle(interval=1.0, clock=lambda: clock[0])
    assert throttle.allow() == (True, 0)
    assert throttle.allow() == (False, 0) and throttle.allow() == (False, 0)
    clock[0] = 2.0
    assert throttle.allow() == (True, 2)


# --------------------------------------------------------------------------
# what a task may name


def refused(api, body, status, message):
    answer = api.client.post("/v1/tasks", json=body, headers=alice())
    assert answer.status_code == status, answer.text
    assert message in answer.json()["msg"], answer.text
    assert api.events("tes.task.created") == []
    last = api.events("tes.task.refused")[-1]
    assert last["source"] == "tes" and last["caller"] == "alice"
    assert last["status"] == status
    assert not api.app.state.engine.tasks


def test_an_image_the_node_was_not_told_of_is_refused(api):
    refused(api, task(api, ["true"], executors=[{"image": OTHER_IMAGE, "command": ["true"]}]),
            403, "is not one this node runs")


@pytest.mark.parametrize("image", ["debian:stable-slim", f"docker://{IMAGE}", "",
                                   None, 7, IMAGE.replace("tools", "Tools")])
def test_a_tag_or_a_near_miss_is_refused(api, image):
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["true"], executors=[{"image": image, "command": ["true"]}]))
    assert answer.status_code == 403


@pytest.mark.parametrize("url_text", [
    "https://example.org/x", "ftp://example.org/x", "s3://bucket/key", "gs://b/k",
    "http://127.0.0.1/x", "/plain/path", "file:relative", "data:text/plain,hi",
])
def test_only_file_urls_are_accepted(api, url_text):
    for where in ("inputs", "outputs"):
        item = {"url": url_text, "path": "/work/x"}
        answer = api.client.post("/v1/tasks", headers=alice(),
                                 json=task(api, ["true"], **{where: [item]}))
        assert answer.status_code in (400, 403), (where, url_text, answer.text)
    assert api.events("tes.task.created") == []


@pytest.mark.parametrize("suffix", ["?x=1", "#frag", ";params"])
def test_a_file_url_is_just_a_path(api, suffix):
    item = {"url": url(api.exchange / "in.txt") + suffix, "path": "/work/in.txt"}
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"], inputs=[item]))
    assert answer.status_code == 400


def test_a_file_url_names_this_host(api):
    (api.exchange / "in.txt").write_text("x")
    item = {"url": f"file://elsewhere{api.exchange}/in.txt", "path": "/work/in.txt"}
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"], inputs=[item]))
    assert answer.status_code == 400 and "this host" in answer.json()["msg"]


@pytest.mark.parametrize("name", ["/etc/passwd", "/tmp", "/", "{exchange}/../outside.txt",
                                  "{exchange}/./a", "{exchange}//a"])
def test_a_path_outside_the_listed_directories_is_refused(api, name):
    path = name.format(exchange=api.exchange)
    for where in ("inputs", "outputs"):
        item = {"url": url(path), "path": "/work/x"}
        answer = api.client.post("/v1/tasks", headers=alice(),
                                 json=task(api, ["true"], **{where: [item]}))
        assert answer.status_code in (400, 403, 404), (where, path, answer.text)
    assert api.events("tes.task.created") == []


def test_an_input_behind_a_symlink_is_refused_even_inside_the_directory(api):
    (api.exchange / "real.txt").write_text("x")
    (api.exchange / "link.txt").symlink_to(api.exchange / "real.txt")
    refused(api, task(api, ["true"], inputs=[{"url": url(api.exchange / "link.txt"),
                                              "path": "/work/in.txt"}]),
            403, "symbolic link")


def test_a_symlink_to_the_datasets_is_refused(api):
    (api.exchange / "sneaky").symlink_to(api.data)
    refused(api, task(api, ["true"], inputs=[{"url": url(api.exchange / "sneaky/cohort"),
                                              "path": "/work/in", "type": "DIRECTORY"}]),
            403, "symbolic link")


def test_the_datasets_are_refused_by_name(api):
    refused(api, task(api, ["true"], inputs=[{"url": url(api.data / "cohort/patients.txt"),
                                              "path": "/work/p.txt"}]),
            403, "not in a directory this node lets tasks use")


def test_a_denied_path_is_refused_even_inside_an_allowed_directory(api):
    inner = api.exchange / "node-data"
    inner.mkdir()
    (inner / "keys.txt").write_text("synthetic")
    config = api.config()._replace(denied=(str(inner.resolve()),))
    policy = Policy(config)
    with pytest.raises(Refused, match="where the node keeps its own data"):
        policy.source(url(inner.resolve() / "keys.txt"))
    with pytest.raises(Refused, match="where the node keeps its own data"):
        policy.destination(url(inner.resolve() / "new.txt"))


def test_an_output_must_land_in_an_existing_listed_directory(api):
    refused(api, task(api, ["true"], outputs=[{"url": url(api.exchange / "no/such/out.txt"),
                                               "path": "/work/out.txt"}]),
            400, "must be an existing directory")


def test_an_output_that_is_a_symlink_is_refused(api):
    (api.exchange / "target.txt").write_text("x")
    (api.exchange / "out.txt").symlink_to(api.exchange / "target.txt")
    refused(api, task(api, ["true"], outputs=[{"url": url(api.exchange / "out.txt"),
                                               "path": "/work/out.txt"}]),
            403, "symbolic link")


@pytest.mark.parametrize("change,message", [
    ({"volumes": ["/:/host"]}, "volumes"),
    ({"resources": {"cpu_cores": 4}}, "resource"),
    ({"id": "forced", "state": "COMPLETE"}, "not understood"),
    ({"executors": []}, "exactly one executor"),
    ({"executors": [{"image": IMAGE, "command": ["a"]}] * 2}, "exactly one executor"),
    ({"executors": [{"image": IMAGE, "command": ["a"], "env": {"K": "V"}}]}, "env"),
    ({"executors": [{"image": IMAGE, "command": ["a"], "stdin": "/etc/passwd"}]}, "stdin"),
    ({"executors": [{"image": IMAGE, "command": ["a"], "stdout": "/x"}]}, "stdout"),
    ({"executors": [{"image": IMAGE, "command": ["a"], "ignore_error": True}]}, "ignore_error"),
    ({"executors": [{"image": IMAGE, "command": []}]}, "command"),
    ({"executors": [{"image": IMAGE, "command": "echo hi"}]}, "command"),
    ({"executors": [{"image": IMAGE, "command": [1]}]}, "command"),
    ({"executors": [{"image": IMAGE, "command": ["a\x00b"]}]}, "command"),
    ({"executors": [{"image": IMAGE, "command": ["a"], "workdir": "/etc"}]}, "workdir"),
    ({"tags": {"k": 1}}, "tags"),
    ({"name": "x" * 129}, "name"),
])
def test_what_would_put_something_in_the_step_or_is_not_honoured_is_refused(
        api, change, message):
    body = task(api, ["true"])
    body.update(change)
    answer = api.client.post("/v1/tasks", headers=alice(), json=body)
    assert answer.status_code in (400, 403), answer.text
    assert message in answer.json()["msg"]
    assert api.events("tes.task.created") == []


@pytest.mark.parametrize("path", ["x", "/other/x", "/work/../x", "/work/./x", "/work//x",
                                  "/work/Snakefile", "/work/.snakemake/meta", "/work",
                                  "/workfoo"])
def test_a_task_path_is_a_plain_one_under_work(api, path):
    (api.exchange / "in.txt").write_text("x")
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["true"], inputs=[{"url": url(api.exchange / "in.txt"), "path": path}]))
    assert answer.status_code in (400, 403), answer.text


def test_an_input_is_a_url_or_content_and_not_both_or_neither(api):
    (api.exchange / "in.txt").write_text("x")
    for item in ({"path": "/work/a"},
                 {"path": "/work/a", "url": url(api.exchange / "in.txt"), "content": "x"}):
        answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"], inputs=[item]))
        assert answer.status_code == 400 and "exactly one of url and content" in answer.text


def test_a_directory_input_must_be_a_directory(api):
    (api.exchange / "file.txt").write_text("x")
    refused(api, task(api, ["true"], inputs=[{"url": url(api.exchange / "file.txt"),
                                              "path": "/work/d", "type": "DIRECTORY"}]),
            400, "not the DIRECTORY")


def test_two_inputs_may_not_overlap(api):
    (api.exchange / "a.txt").write_text("x")
    items = [{"url": url(api.exchange / "a.txt"), "path": "/work/a"},
             {"url": url(api.exchange / "a.txt"), "path": "/work/a/b"}]
    refused(api, task(api, ["true"], inputs=items), 400, "overlap")


def test_a_body_that_is_not_a_task_is_refused(api):
    for body in ([], "text", 7, None):
        answer = api.client.post("/v1/tasks", headers=alice(), json=body)
        assert answer.status_code == 400
    answer = api.client.post("/v1/tasks", headers={**alice(), "Content-Type": "application/json"},
                             content=b"{not json")
    assert answer.status_code == 400


def test_a_large_body_is_turned_away(api):
    answer = api.client.post("/v1/tasks", headers=alice(),
                             content=b'{"x":"' + b"a" * (2 * 1024 * 1024) + b'"}')
    assert answer.status_code == 413


# --------------------------------------------------------------------------
# a task runs, with the node's runner


def test_a_task_runs_and_its_output_is_where_it_asked(api):
    (api.exchange / "in.txt").write_text("alpha\n")
    out = api.exchange / "out.txt"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "tr a-z A-Z < in.txt > out.txt; echo said-this; echo and-this >&2"],
        inputs=[{"url": url(api.exchange / "in.txt"), "path": "/work/in.txt"}],
        outputs=[{"url": url(out), "path": "/work/out.txt"}]))
    assert created.status_code == 200, created.text
    done = wait(api, created.json()["id"])
    assert done["state"] == "COMPLETE", done
    assert out.read_text() == "ALPHA\n"
    assert not out.is_symlink() and oct(out.stat().st_mode & 0o777) == "0o640"
    log = done["logs"][0]["logs"][0]
    assert "said-this" in log["stdout"] and "and-this" in log["stderr"] and log["exit_code"] == 0
    assert done["logs"][0]["outputs"][0]["size_bytes"] == str(len("ALPHA\n"))
    assert os.listdir(api.runs) == [], "the workspace is removed"


def test_the_runner_is_the_nodes_and_is_asked_for_the_image(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    wait(api, created.json()["id"])
    assert api.runner.used == [IMAGE]


def test_arguments_that_name_work_name_the_workspace(api):
    (api.exchange / "in.txt").write_text("beta\n")
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "cat /work/in.txt > /work/out.txt"],
        inputs=[{"url": url(api.exchange / "in.txt"), "path": "/work/in.txt"}],
        outputs=[{"url": url(api.exchange / "out.txt"), "path": "/work/out.txt"}]))
    done = wait(api, created.json()["id"])
    # "sh -c" has one argument that is a script, and the script is the caller's:
    # only an argument that starts with /work is the workspace's path.
    assert done["state"] == "EXECUTOR_ERROR"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["cp", "/work/in.txt", "/work/out.txt"],
        inputs=[{"url": url(api.exchange / "in.txt"), "path": "/work/in.txt"}],
        outputs=[{"url": url(api.exchange / "out.txt"), "path": "/work/out.txt"}]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert (api.exchange / "out.txt").read_text() == "beta\n"


def test_braces_and_quotes_in_a_command_reach_the_tool_unchanged(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "printf '%s' \"{a} {{b}} 'q' $(echo no)\" > out.txt"],
        outputs=[{"url": url(api.exchange / "out.txt"), "path": "/work/out.txt"}]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert (api.exchange / "out.txt").read_text() == "{a} {{b}} 'q' no"


def test_inline_content_and_directories_work(api):
    source = api.exchange / "bundle"
    (source / "sub").mkdir(parents=True)
    (source / "one.txt").write_text("1")
    (source / "sub" / "two.txt").write_text("2")
    target = api.exchange / "result"
    target.mkdir()
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "mkdir -p made/deep && cat bundle/one.txt bundle/sub/two.txt "
                          "note.txt > made/all.txt && echo x > made/deep/x.txt"],
        inputs=[{"url": url(source), "path": "/work/bundle", "type": "DIRECTORY"},
                {"content": "-note", "path": "/work/note.txt"}],
        outputs=[{"url": url(target), "path": "/work/made", "type": "DIRECTORY"}]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert (target / "all.txt").read_text() == "12-note"
    assert (target / "deep" / "x.txt").read_text() == "x\n"


def test_a_failing_command_writes_nothing(api):
    out = api.exchange / "out.txt"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "echo partial > out.txt; echo why >&2; exit 4"],
        outputs=[{"url": url(out), "path": "/work/out.txt"}]))
    done = wait(api, created.json()["id"])
    assert done["state"] == "EXECUTOR_ERROR"
    assert "why" in done["logs"][0]["logs"][0]["stderr"]
    assert done["logs"][0]["logs"][0]["exit_code"] != 0
    assert not out.exists()


def test_an_output_the_task_did_not_make_is_a_system_error(api):
    out = api.exchange / "out.txt"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["true"], outputs=[{"url": url(out), "path": "/work/out.txt"}]))
    done = wait(api, created.json()["id"])
    assert done["state"] == "SYSTEM_ERROR"
    assert "did not produce /work/out.txt" in done["logs"][0]["system_logs"][0]


def test_too_many_bytes_is_a_system_error(world):
    app = create_app(world.config(BIOCHEF_TESAPI_MAX_BYTES="10"), RecordingRunner())
    (world.exchange / "big.txt").write_text("x" * 100)
    with TestClient(app) as client:
        world.client = client
        created = client.post("/v1/tasks", headers=alice(), json=task(
            world, ["true"], inputs=[{"url": url(world.exchange / "big.txt"),
                                      "path": "/work/big.txt"}]))
        done = wait(world, created.json()["id"])
    assert done["state"] == "SYSTEM_ERROR" and "bytes" in done["logs"][0]["system_logs"][0]


def test_an_output_destination_swapped_for_a_link_is_refused_at_the_end(api):
    out = api.exchange / "out.txt"
    plan = tasks.parse(task(api, ["true"], outputs=[
        {"url": url(out), "path": "/work/out.txt"}]), api.app.state.policy)
    workspace = api.tmp / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("produced")
    (api.exchange / "elsewhere.txt").write_text("keep")
    out.symlink_to(api.exchange / "elsewhere.txt")
    with pytest.raises(Refused, match="symbolic link"):
        tasks.stage_out(plan, str(workspace), tasks.Budget(1000), api.app.state.policy)
    assert (api.exchange / "elsewhere.txt").read_text() == "keep"


def test_an_input_that_is_a_link_inside_a_directory_is_refused_at_the_start(api):
    source = api.exchange / "bundle"
    source.mkdir()
    (source / "ok.txt").write_text("x")
    (source / "link").symlink_to("/etc/passwd")
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["true"], inputs=[{"url": url(source), "path": "/work/b", "type": "DIRECTORY"}]))
    done = wait(api, created.json()["id"])
    assert done["state"] == "SYSTEM_ERROR"
    assert "is not a plain file" in done["logs"][0]["system_logs"][0]


# --------------------------------------------------------------------------
# the audit trail


def test_every_step_of_a_task_is_audited_with_source_tes(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["echo", "a-secret-argument"], name="audited"))
    task_id = created.json()["id"]
    wait(api, task_id)
    events = [e for e in api.events() if e.get("task_id") == task_id]
    assert [e["event"] for e in events][0] == "tes.task.created"
    states = [e["state"] for e in events if e["event"] == "tes.task.state"]
    assert states == ["INITIALIZING", "RUNNING", "COMPLETE"]
    assert all(e["source"] == "tes" and e["caller"] == "alice" for e in events)
    assert events[0]["image"] == IMAGE
    assert re.fullmatch(r"[0-9a-f]{64}", events[0]["command_sha256"])
    assert "a-secret-argument" not in api.trail.read_text(), "arguments may hold secrets"
    assert TOKEN_A not in api.trail.read_text()


def test_a_node_that_cannot_record_does_not_run(api, monkeypatch):
    def broken(*args, **kwargs):
        raise audit.AuditError("disk full")
    monkeypatch.setattr(audit, "record", broken)
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    assert answer.status_code == 503 and "audit trail" in answer.json()["msg"]
    assert not api.app.state.engine.tasks
    assert api.app.state.engine.active == 0


# --------------------------------------------------------------------------
# callers see their own tasks, and cancel them


def test_one_caller_does_not_see_anothers_task(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"], name="mine"))
    task_id = created.json()["id"]
    wait(api, task_id)
    assert api.client.get(f"/v1/tasks/{task_id}", headers=bob()).status_code == 404
    assert api.client.post(f"/v1/tasks/{task_id}:cancel", headers=bob()).status_code == 404
    assert api.client.get("/v1/tasks", headers=bob()).json()["tasks"] == []
    mine = api.client.get("/v1/tasks?name_prefix=mi", headers=alice()).json()["tasks"]
    assert [t["id"] for t in mine] == [task_id]


def test_the_views_and_the_list_page(api):
    ids = []
    for number in range(3):
        created = api.client.post("/v1/tasks", headers=alice(),
                                  json=task(api, ["true"], name=f"n{number}"))
        ids.append(created.json()["id"])
    for task_id in ids:
        wait(api, task_id)
    minimal = api.client.get(f"/v1/tasks/{ids[0]}", headers=alice()).json()
    assert set(minimal) == {"id", "state"}
    basic = api.client.get(f"/v1/tasks/{ids[0]}?view=BASIC", headers=alice()).json()
    assert "logs" not in basic and basic["executors"][0]["image"] == IMAGE
    page = api.client.get("/v1/tasks?page_size=2&view=MINIMAL", headers=alice()).json()
    assert len(page["tasks"]) == 2 and page["next_page_token"] == "2"
    rest = api.client.get("/v1/tasks?page_size=2&page_token=2", headers=alice()).json()
    assert len(rest["tasks"]) == 1 and rest["next_page_token"] == ""
    assert api.client.get("/v1/tasks?view=NOPE", headers=alice()).status_code == 400
    assert api.client.get("/v1/tasks?state=COMPLETE", headers=alice()).json()["tasks"]


def test_a_running_task_can_be_cancelled(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["sleep", "120"]))
    task_id = created.json()["id"]
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if api.client.get(f"/v1/tasks/{task_id}", headers=alice()).json()["state"] == "RUNNING":
            break
        time.sleep(0.2)
    assert api.client.post(f"/v1/tasks/{task_id}:cancel", headers=alice()).status_code == 200
    assert wait(api, task_id)["state"] == "CANCELED"
    assert api.events("tes.task.state")[-1]["state"] == "CANCELED"


# --------------------------------------------------------------------------
# a queue and slots of its own


def test_the_queue_is_bounded_and_says_so(world):
    app = create_app(world.config(BIOCHEF_TESAPI_MAX_CONCURRENT="1",
                                  BIOCHEF_TESAPI_MAX_QUEUED="1"), RecordingRunner())
    with TestClient(app) as client:
        world.client = client
        first = client.post("/v1/tasks", headers=alice(), json=task(world, ["sleep", "120"]))
        second = client.post("/v1/tasks", headers=alice(), json=task(world, ["sleep", "120"]))
        third = client.post("/v1/tasks", headers=alice(), json=task(world, ["true"]))
        assert (first.status_code, second.status_code, third.status_code) == (200, 200, 503)
        assert third.headers["retry-after"] == "5"
        assert world.events("tes.task.refused")[-1]["reason"] == "queue full"
        client.post(f"/v1/tasks/{second.json()['id']}:cancel", headers=alice())
        assert client.get(f"/v1/tasks/{second.json()['id']}", headers=alice()).json()[
            "state"] == "CANCELED"
        again = client.post("/v1/tasks", headers=alice(), json=task(world, ["true"]))
        assert again.status_code == 200, "a cancelled place is free again"
        client.post(f"/v1/tasks/{first.json()['id']}:cancel", headers=alice())
        wait(world, first.json()["id"])


def test_the_slots_are_not_the_agents(api):
    engine = api.app.state.engine
    assert engine.config.max_concurrent == 1
    code = ("import sys; import tesapi.app, tesapi.tasks, tesapi.__main__; "
            "bad = [m for m in ('main', 'ui', 'node', 'store', 'runs', 'sqlmodel') "
            "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_listener_never_opens_the_nodes_database_or_the_other_apps():
    for source in (REPO_ROOT / "tesapi").glob("*.py"):
        text = source.read_text()
        for forbidden in (r"^\s*(from|import)\s+(store|node|ui|main|runs)\b",
                          r"sqlmodel", r"sqlalchemy"):
            assert not re.search(forbidden, text, re.M), (source.name, forbidden)


# --------------------------------------------------------------------------
# the runner the tasks go through, and its guarantees


def test_the_listener_refuses_a_runner_the_isolation_rule_refuses(world, monkeypatch):
    from tesapi.__main__ import prepare
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    with pytest.raises(settings.ConfigError, match="BIOCHEF_REQUIRE_NETWORK_ISOLATION"):
        prepare(world.config(), RecordingRunner(network=ATTESTED))
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    with pytest.raises(settings.ConfigError, match="cannot run an image chosen"):
        prepare(world.config(), SubprocessRunner())
    prepare(world.config(), RecordingRunner(network=ATTESTED))


def test_an_enforced_runner_is_accepted(world, monkeypatch):
    from tesapi.__main__ import prepare
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    prepare(world.config(), RecordingRunner(network=ENFORCED))


def test_the_start_checks_are_audited_with_source_tes(world, monkeypatch):
    from tesapi.__main__ import start_checks
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    start_checks(world.config(), RecordingRunner())()
    events = world.events()
    assert [e["event"] for e in events] == ["node.runner.isolation_not_required",
                                            "tes.start"]
    assert all(e["source"] == "tes" for e in events)
    assert events[1]["images"] == [IMAGE]


def test_apptainer_and_podman_run_the_image_the_task_names():
    wanted = f"registry.example/t@sha256:{DIGEST}"
    other = ApptainerRunner().with_image(wanted)
    assert other.image == f"docker://{wanted}" and other.guarantees == ApptainerRunner().guarantees
    assert f'container: "docker://{wanted}"' in other.snakefile_preamble()
    assert ApptainerRunner().image != other.image, "the original is untouched"


def test_a_with_image_runner_shares_the_binary_directory():
    base = ApptainerRunner()
    first, second = base.with_image(IMAGE), base.with_image(OTHER_IMAGE)
    assert first.environment()["PATH"] == second.environment()["PATH"] == base.environment()["PATH"]


def test_rootless_podman_follows_the_task_and_rootful_does_not(monkeypatch):
    monkeypatch.setenv("BIOCHEF_PODMAN_MODE", "rootless")
    monkeypatch.setenv("BIOCHEF_PODMAN_SELINUX", "disable")
    monkeypatch.setenv("BIOCHEF_STEP_PIDS", "64")
    monkeypatch.setenv("BIOCHEF_STEP_MEMORY", "1g")
    base = PodmanRunner()
    assert base.with_image(IMAGE).image == f"docker://{IMAGE}"
    rootful = PodmanRunner.__new__(PodmanRunner)
    rootful.mode, rootful.image = "rootful", f"docker://{IMAGE}"
    assert rootful.with_image(IMAGE).image == f"docker://{IMAGE}"
    with pytest.raises(ValueError, match="only root can change"):
        rootful.with_image(OTHER_IMAGE)


def test_the_tes_runner_follows_the_task_too():
    from runner import TesRunner
    import tes
    base = TesRunner.__new__(TesRunner)
    base.settings = tes.Settings("https://tes.example", IMAGE, "none", None, None, "/s", "/h")
    base.image = IMAGE
    other = base.with_image(OTHER_IMAGE)
    assert other.image == OTHER_IMAGE and other.settings.image == OTHER_IMAGE
    assert base.image == IMAGE
    with pytest.raises(tes.TesError):
        base.with_image("debian:stable-slim")


def test_a_runner_that_runs_one_image_says_so():
    with pytest.raises(ValueError, match="cannot run an image chosen"):
        SubprocessRunner().with_image(IMAGE)
