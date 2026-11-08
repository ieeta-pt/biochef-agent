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
import shutil
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


@pytest.fixture(scope="session")
def migrated_database(tmp_path_factory):
    """The node's database at the current schema, made once and copied for each test."""
    from store import migrate
    path = tmp_path_factory.mktemp("template") / "agent.db"
    migrate.upgrade(f"sqlite:///{path}")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch, migrated_database):
    """A node's directories, a token file, an audit trail, and a settings mapping."""
    shutil.copy(migrated_database, tmp_path / "agent.db")
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()
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
        "BIOCHEF_TESAPI_IMAGES": IMAGE, "BIOCHEF_TESAPI_READ_DIRS": str(exchange),
        "BIOCHEF_TESAPI_WRITE_DIRS": str(outbox),
        "BIOCHEF_LOCAL_ROOT": str(data), "BIOCHEF_RUN_ROOT": str(runs),
        "BIOCHEF_TESAPI_TIMEOUT": "120", "BIOCHEF_DB_URL": f"sqlite:///{tmp_path}/agent.db",
    }

    class World:
        pass

    found = World()
    found.tmp, found.exchange, found.data, found.runs = tmp_path, exchange, data, runs
    found.outbox = outbox
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
                                  "BIOCHEF_TESAPI_IMAGES", "BIOCHEF_TESAPI_READ_DIRS",
                                  "BIOCHEF_TESAPI_WRITE_DIRS"])
def test_nothing_that_decides_what_a_task_may_touch_has_a_default(world, name):
    with pytest.raises(settings.ConfigError, match=name):
        world.config(**{name: ""})


def test_it_loads(world):
    config = world.config()
    assert config.images == (IMAGE,) and (config.read_dirs, config.write_dirs) == (
        (str(world.exchange.resolve()),), (str(world.outbox.resolve()),))
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
@pytest.mark.parametrize("name", ["BIOCHEF_TESAPI_READ_DIRS", "BIOCHEF_TESAPI_WRITE_DIRS"])
def test_a_directory_that_is_not_a_plain_directory_stops_it(world, name, path):
    with pytest.raises(settings.ConfigError):
        world.config(**{name: path})


def test_a_directory_named_through_a_symlink_stops_it(world):
    link = world.tmp / "link"
    link.symlink_to(world.exchange)
    with pytest.raises(settings.ConfigError, match="symlink"):
        world.config(BIOCHEF_TESAPI_READ_DIRS=str(link))
    with pytest.raises(settings.ConfigError, match="symlink"):
        world.config(BIOCHEF_TESAPI_WRITE_DIRS=str(link))


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
        world.config(BIOCHEF_TESAPI_READ_DIRS=str(allowed))
    with pytest.raises(settings.ConfigError, match="overlaps"):
        world.config(BIOCHEF_TESAPI_WRITE_DIRS=str(allowed))


def test_a_directory_beside_the_datasets_is_fine(world):
    side = world.tmp / "datasets-public"
    side.mkdir()
    assert world.config(BIOCHEF_TESAPI_READ_DIRS=str(side.resolve())).read_dirs == (
        str(side.resolve()),)
    assert world.config(BIOCHEF_TESAPI_WRITE_DIRS=str(side.resolve())).write_dirs == (
        str(side.resolve()),)


def test_the_two_lists_may_share_no_directory(world):
    a, b = world.exchange.resolve(), world.outbox.resolve()
    inside = a / "deeper"
    inside.mkdir()
    for read, write in ((a, a), (a, inside), (inside, a), (a, f"{b},{a}")):
        with pytest.raises(settings.ConfigError, match="share a directory"):
            world.config(BIOCHEF_TESAPI_READ_DIRS=str(read), BIOCHEF_TESAPI_WRITE_DIRS=str(write))
    sibling = world.tmp / "exchange-2"
    sibling.mkdir()
    config = world.config(BIOCHEF_TESAPI_READ_DIRS=f"{a},{sibling.resolve()}",
                          BIOCHEF_TESAPI_WRITE_DIRS=str(b))
    assert config.read_dirs == (str(a), str(sibling.resolve()))


def test_the_old_single_list_is_refused_and_says_what_replaces_it(world):
    with pytest.raises(settings.ConfigError, match="BIOCHEF_TESAPI_READ_DIRS"):
        world.config(BIOCHEF_TESAPI_DIRS=str(world.exchange))


def test_the_default_database_is_a_place_no_task_may_use(world, monkeypatch):
    """With BIOCHEF_DB_URL unset the agent uses the default file, so it is denied too."""
    from store import db
    default = db.DEFAULT_URL.split(":", 1)[1].lstrip("/")
    assert ("BIOCHEF_DB_URL", os.path.realpath("/" + default)) in settings._denied({})


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
    assert info["storage"] == [f"file://{api.exchange.resolve()}",
                               f"file://{api.outbox.resolve()}"]


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
            403, "not in a directory this node lets tasks read")


def test_a_denied_path_is_refused_even_inside_an_allowed_directory(api):
    inner = api.exchange / "node-data"
    inner.mkdir()
    (inner / "keys.txt").write_text("synthetic")
    inner_out = api.outbox / "node-data"
    inner_out.mkdir()
    config = api.config()._replace(denied=(str(inner.resolve()), str(inner_out.resolve())))
    policy = Policy(config)
    with pytest.raises(Refused, match="where the node keeps its own data"):
        policy.source(url(inner.resolve() / "keys.txt"))
    with pytest.raises(Refused, match="where the node keeps its own data"):
        policy.destination(url(inner_out.resolve() / "new.txt"))


def test_what_a_task_may_read_it_may_not_write_and_the_other_way_round(api):
    (api.outbox / "written.txt").write_text("x")
    refused(api, task(api, ["true"], inputs=[{"url": url(api.outbox / "written.txt"),
                                              "path": "/work/x"}]),
            403, "not in a directory this node lets tasks read")
    refused(api, task(api, ["true"], outputs=[{"url": url(api.exchange / "back.txt"),
                                               "path": "/work/x"}]),
            403, "not in a directory this node lets tasks write")


def test_an_output_must_land_in_an_existing_listed_directory(api):
    refused(api, task(api, ["true"], outputs=[{"url": url(api.outbox / "no/such/out.txt"),
                                               "path": "/work/out.txt"}]),
            400, "must be an existing directory")


def test_an_output_that_is_a_symlink_is_refused(api):
    (api.outbox / "target.txt").write_text("x")
    (api.outbox / "out.txt").symlink_to(api.outbox / "target.txt")
    refused(api, task(api, ["true"], outputs=[{"url": url(api.outbox / "out.txt"),
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
    out = api.outbox / "out.txt"
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
        outputs=[{"url": url(api.outbox / "out.txt"), "path": "/work/out.txt"}]))
    done = wait(api, created.json()["id"])
    # "sh -c" has one argument that is a script, and the script is the caller's:
    # only an argument that starts with /work is the workspace's path.
    assert done["state"] == "EXECUTOR_ERROR"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["cp", "/work/in.txt", "/work/out.txt"],
        inputs=[{"url": url(api.exchange / "in.txt"), "path": "/work/in.txt"}],
        outputs=[{"url": url(api.outbox / "out.txt"), "path": "/work/out.txt"}]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert (api.outbox / "out.txt").read_text() == "beta\n"


def test_braces_and_quotes_in_a_command_reach_the_tool_unchanged(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "printf '%s' \"{a} {{b}} 'q' $(echo no)\" > out.txt"],
        outputs=[{"url": url(api.outbox / "out.txt"), "path": "/work/out.txt"}]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert (api.outbox / "out.txt").read_text() == "{a} {{b}} 'q' no"


def test_inline_content_and_directories_work(api):
    source = api.exchange / "bundle"
    (source / "sub").mkdir(parents=True)
    (source / "one.txt").write_text("1")
    (source / "sub" / "two.txt").write_text("2")
    target = api.outbox / "result"
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
    out = api.outbox / "out.txt"
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "echo partial > out.txt; echo why >&2; exit 4"],
        outputs=[{"url": url(out), "path": "/work/out.txt"}]))
    done = wait(api, created.json()["id"])
    assert done["state"] == "EXECUTOR_ERROR"
    assert "why" in done["logs"][0]["logs"][0]["stderr"]
    assert done["logs"][0]["logs"][0]["exit_code"] != 0
    assert not out.exists()


def test_an_output_the_task_did_not_make_is_a_system_error(api):
    out = api.outbox / "out.txt"
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
    out = api.outbox / "out.txt"
    plan = tasks.parse(task(api, ["true"], outputs=[
        {"url": url(out), "path": "/work/out.txt"}]), api.app.state.policy)
    workspace = api.tmp / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("produced")
    (api.outbox / "elsewhere.txt").write_text("keep")
    out.symlink_to(api.outbox / "elsewhere.txt")
    with pytest.raises(Refused, match="symbolic link"):
        tasks.stage_out(plan, str(workspace), tasks.Budget(1000), api.app.state.policy)
    assert (api.outbox / "elsewhere.txt").read_text() == "keep"


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
# tasks outlive the process that ran them


def restarted(world, **changes):
    """A second listener on the same database, as after a restart: the first has
    stopped and given its place up, which is what a clean shutdown does."""
    first = world.app.state.engine
    first.close()
    first.lost = True       # it takes no further part: no write, no new task
    app = create_app(world.config(**changes), RecordingRunner())
    return TestClient(app)


def stored(world):
    from sqlmodel import select
    from store import db
    from store.models import TesTask
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    with db.reading(engine) as session:
        return {row.id: row for row in session.exec(select(TesTask)).all()}


def test_a_finished_task_is_still_there_after_a_restart(api):
    (api.exchange / "in.txt").write_text("alpha\n")
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sh", "-c", "echo said-this; echo and-this >&2; cp in.txt out.txt"],
        name="kept", tags={"k": "v"}, description="d",
        inputs=[{"url": url(api.exchange / "in.txt"), "path": "/work/in.txt"},
                {"content": "inline-secret-looking-text", "path": "/work/note.txt"}],
        outputs=[{"url": url(api.outbox / "out.txt"), "path": "/work/out.txt"}]))
    task_id = created.json()["id"]
    before = wait(api, task_id)
    assert before["state"] == "COMPLETE"
    with restarted(api) as client:
        after = client.get(f"/v1/tasks/{task_id}?view=FULL", headers=alice())
        assert after.status_code == 200
        assert after.json() == before, "everything the caller was told is the same"
        listed = client.get("/v1/tasks?name_prefix=ke", headers=alice()).json()["tasks"]
        assert [t["id"] for t in listed] == [task_id]
        assert client.get(f"/v1/tasks/{task_id}", headers=bob()).status_code == 404, \
            "another caller's task is still not visible"


def test_the_inline_content_of_an_input_is_not_kept(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["true"], inputs=[{"content": "inline-secret-looking-text",
                                "path": "/work/note.txt"}]))
    wait(api, created.json()["id"])
    row = stored(api)[created.json()["id"]]
    assert row.inputs == []
    import sqlite3
    with sqlite3.connect(str(api.tmp / "agent.db")) as connection:
        dump = "\n".join(connection.iterdump())
    assert "inline-secret-looking-text" not in dump
    assert TOKEN_A not in dump


@pytest.mark.parametrize("was", ["QUEUED", "INITIALIZING", "RUNNING", "CANCELING"])
def test_a_task_that_was_unfinished_at_a_restart_is_a_system_error(api, was):
    from datetime import datetime, timezone
    from store import db
    from store.models import TesTask
    engine = db.make_engine(api.values["BIOCHEF_DB_URL"])
    lost = "lost" + was.lower().ljust(28, "0")
    with db.writing(engine) as session:
        session.add(TesTask(id=lost, owner="alice", name="lost", image=IMAGE,
                            command=["sleep", "999"], state=was,
                            created_at=datetime.now(timezone.utc)))
    with restarted(api) as client:
        view = client.get(f"/v1/tasks/{lost}?view=FULL", headers=alice()).json()
    assert view["state"] == "SYSTEM_ERROR"
    assert f"stopped while this task was {was}" in view["logs"][0]["system_logs"][0]
    assert stored(api)[lost].state == "SYSTEM_ERROR" and stored(api)[lost].ended_at
    event, = [e for e in api.events("tes.task.state") if e.get("task_id") == lost]
    assert (event["state"], event["error"], event["was"], event["source"]) == (
        "SYSTEM_ERROR", "restarted", was, "tes")


def test_a_cancelled_task_stays_cancelled(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sleep", "30"]))
    task_id = created.json()["id"]
    for _ in range(100):
        if api.client.get(f"/v1/tasks/{task_id}", headers=alice()).json()["state"] == "RUNNING":
            break
        time.sleep(0.1)
    api.client.post(f"/v1/tasks/{task_id}:cancel", headers=alice())
    assert wait(api, task_id)["state"] == "CANCELED"
    with restarted(api) as client:
        assert client.get(f"/v1/tasks/{task_id}", headers=alice()).json()["state"] == "CANCELED"


def test_the_listener_writes_to_two_tables_only(api):
    created = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    wait(api, created.json()["id"])
    import sqlite3
    with sqlite3.connect(str(api.tmp / "agent.db")) as connection:
        tables = [name for (name,) in connection.execute(
            "select name from sqlite_master where type='table' and name not like "
            "'sqlite_%' and name != 'alembic_version'")]
        counts = {name: connection.execute(f"select count(*) from {name}").fetchone()[0]
                  for name in tables}
    assert counts.pop("tes_task") == 1 and counts.pop("tes_listener") == 1
    assert not any(counts.values()), counts


def test_a_task_the_database_cannot_keep_does_not_run(api, monkeypatch):
    from tesapi import records
    def refuse(bind, task):
        raise records.RecordError("the task could not be kept: OperationalError")
    monkeypatch.setattr(records, "save", refuse)
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    assert answer.status_code == 503 and "will not run it" in answer.json()["msg"]
    assert api.runner.used == [], "nothing ran"
    assert api.app.state.engine.active == 0, "and it holds no place in the queue"


def test_a_database_at_an_older_schema_stops_the_listener(world, capsys, monkeypatch):
    from store import migrate
    from tesapi.__main__ import main
    migrate.downgrade(world.values["BIOCHEF_DB_URL"], "0005")
    for key, value in world.values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BIOCHEF_RUNNER", "subprocess")
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    # prepare() would refuse the subprocess runner first; the schema is checked after
    # it, so name a runner that passes it.
    import tesapi.__main__ as entry
    monkeypatch.setattr(entry, "get_runner", lambda name: RecordingRunner(network=ATTESTED))
    assert main([]) == 1
    assert "schema is not current" in capsys.readouterr().err


def test_old_finished_tasks_are_forgotten_in_the_database_too(api, monkeypatch):
    monkeypatch.setattr(tasks, "MAX_KEPT_TASKS", 2)
    ids = []
    for number in range(4):
        created = api.client.post("/v1/tasks", headers=alice(), json=task(
            api, ["true"], name=f"n{number}"))
        ids.append(created.json()["id"])
        wait(api, ids[-1])
    assert set(stored(api)) == set(ids[-2:])
    assert [t["id"] for t in api.client.get("/v1/tasks", headers=alice()).json()["tasks"]] == \
        list(reversed(ids[-2:]))


def _row(identifier, state, minute, owner="alice"):
    from datetime import datetime, timedelta, timezone
    from store.models import TesTask
    return TesTask(id=identifier, owner=owner, name=identifier, image=IMAGE,
                   command=["true"], state=state,
                   created_at=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
                   + timedelta(minutes=minute))


def test_a_task_is_still_its_owners_after_a_restart(api):
    from store import db
    engine = db.make_engine(api.values["BIOCHEF_DB_URL"])
    with db.writing(engine) as session:
        session.add(_row("bobs-task-0000000000000000000000", "COMPLETE", 1, owner="bob"))
    with restarted(api) as client:
        assert client.get("/v1/tasks/bobs-task-0000000000000000000000",
                          headers=bob()).status_code == 200
        assert client.get("/v1/tasks/bobs-task-0000000000000000000000",
                          headers=alice()).status_code == 404
        assert client.get("/v1/tasks", headers=alice()).json()["tasks"] == []


def test_pruning_never_drops_an_unfinished_task(world):
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    with db.writing(engine) as session:
        session.add(_row("a-running", "RUNNING", 0))
        session.add(_row("b-done", "COMPLETE", 1))
        session.add(_row("c-done", "COMPLETE", 2))
        session.add(_row("d-done", "CANCELED", 3))
    records.prune(engine, 2)
    assert {row.id for row in records.load(engine, 10)} == {"a-running", "d-done"}, \
        "the two oldest finished went; the oldest task of all, unfinished, stayed"
    records.prune(engine, 1)
    assert {row.id for row in records.load(engine, 10)} == {"a-running"}


def test_the_newest_tasks_are_the_ones_read_back(world):
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    with db.writing(engine) as session:
        for minute in range(5):
            session.add(_row(f"t{minute}", "COMPLETE", minute))
    assert [row.id for row in records.load(engine, 3)] == ["t2", "t3", "t4"]


def test_a_task_being_cancelled_is_written_down_as_such(api):
    from store import db
    from tesapi import records
    from tesapi.tasks import Engine, Task, parse
    engine = Engine(api.config(), api.app.state.policy, RecordingRunner(),
                    db.make_engine(api.values["BIOCHEF_DB_URL"]))
    live = Task("alice", parse(task(api, ["true"]), api.app.state.policy))
    live.state = "RUNNING"
    records.save(engine.bind, live)
    engine.cancel(live)
    assert [row.state for row in records.load(engine.bind, 5)] == ["CANCELING"]


def test_the_database_refuses_a_state_that_is_not_a_tes_state(world):
    from sqlalchemy.exc import IntegrityError
    from store import db
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    with pytest.raises(IntegrityError):
        with db.writing(engine) as session:
            session.add(_row("odd", "PAUSED", 0))


def test_a_listener_started_on_an_older_schema_does_not_take_requests(world):
    from store import migrate
    from tesapi import records
    migrate.downgrade(world.values["BIOCHEF_DB_URL"], "0005")
    app = create_app(world.config(), RecordingRunner())
    with pytest.raises(records.RecordError, match="schema is not current"):
        with TestClient(app):
            pass


# --------------------------------------------------------------------------
# one listener per database


def lease_row(world):
    from sqlmodel import select
    from store import db
    from store.models import TesListener
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    with db.reading(engine) as session:
        return session.exec(select(TesListener)).all()


def test_a_second_listener_on_the_same_database_refuses_to_start(api):
    from tesapi import records
    second = create_app(api.config(), RecordingRunner())
    with pytest.raises(records.Held, match="another TES listener holds this database"):
        with TestClient(second):
            pass
    row, = lease_row(api)
    assert row.pid == os.getpid() and row.holder == api.app.state.engine.holder
    # The first goes on as it was: its tasks are not ended, and it takes new ones.
    created = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    assert wait(api, created.json()["id"])["state"] == "COMPLETE"
    assert [e["state"] for e in api.events("tes.task.state")
            if e.get("error") == "restarted"] == []


def test_the_refusal_says_who_holds_it_and_until_when(api):
    from tesapi import records
    with pytest.raises(records.Held) as refused:
        records.preflight(api.app.state.engine.bind)
    message = str(refused.value)
    assert f"pid {os.getpid()}" in message and "wait until" in message
    assert "end the first one's running tasks" in message


def test_a_clean_stop_gives_the_place_up_at_once(api):
    from tesapi import records
    api.app.state.engine.close()
    assert lease_row(api) == []
    with TestClient(create_app(api.config(), RecordingRunner())):
        assert records.current(api.app.state.engine.bind) is not None


def test_a_listener_that_was_killed_is_replaced_when_its_place_expires(api):
    from datetime import datetime, timedelta, timezone
    from store import db
    from store.models import TesListener, TesTask
    from tesapi import records
    first = api.app.state.engine
    first.lost = True                       # killed: no release, no more writes
    engine = db.make_engine(api.values["BIOCHEF_DB_URL"])
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=records.LEASE_S + 5)
    with db.writing(engine) as session:
        row = session.get(TesListener, 1)
        row.heartbeat_at = long_ago
        session.add(row)
        session.add(_row("orphan-0000000000000000000000000", "RUNNING", 0))
    with pytest.raises(records.Held):       # not yet expired, for a listener that is alive
        records.preflight(engine, now=long_ago + timedelta(seconds=records.LEASE_S - 1))
    with TestClient(create_app(api.config(), RecordingRunner())) as client:
        view = client.get("/v1/tasks/orphan-0000000000000000000000000", headers=alice())
        assert view.json()["state"] == "SYSTEM_ERROR"
    taken, = [e for e in api.events("tes.lock.acquired") if e["took_over_from"]]
    assert taken["took_over_from"]["pid"] == os.getpid()


def test_the_place_expires_exactly_when_the_lease_has_run_out(world):
    from datetime import datetime, timedelta, timezone
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    start = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    records.acquire(engine, "one", now=start)
    for seconds, free in ((records.LEASE_S - 1, False), (records.LEASE_S, True),
                          (records.LEASE_S + 1, True)):
        moment = start + timedelta(seconds=seconds)
        assert (records.current(engine, now=moment) is None) is free
    with pytest.raises(records.Held):
        records.acquire(engine, "two", now=start + timedelta(seconds=records.LEASE_S - 1))
    assert records.acquire(engine, "two", now=start + timedelta(seconds=records.LEASE_S)) \
        is not None, "and it says whose place it took"


def test_the_holder_may_take_its_own_place_again(world):
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    assert records.acquire(engine, "one") is None
    assert records.acquire(engine, "one") is None


def test_listeners_that_start_together_cannot_both_win(world):
    import threading
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    results, barrier = [], threading.Barrier(8)

    def start(number):
        barrier.wait()
        try:
            records.acquire(engine, f"listener-{number}")
            results.append("won")
        except records.Held:
            results.append("held")

    threads = [threading.Thread(target=start, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("won") == 1 and results.count("held") == 7


def test_a_heartbeat_renews_the_place_and_only_for_its_holder(world):
    from datetime import datetime, timedelta, timezone
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    start = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    records.acquire(engine, "one", now=start)
    assert records.beat(engine, "one", now=start + timedelta(seconds=10)) is True
    assert records.beat(engine, "someone-else", now=start + timedelta(seconds=11)) is False
    assert records.current(engine, now=start + timedelta(seconds=10 + records.LEASE_S - 1))
    assert records.current(engine, now=start + timedelta(seconds=10 + records.LEASE_S)) is None


def test_a_running_listener_renews_its_place(api, monkeypatch):
    import asyncio
    engine = api.app.state.engine
    before = lease_row(api)[0].heartbeat_at

    async def beats():
        task = asyncio.create_task(engine.heartbeat(interval=0.05))
        await asyncio.sleep(0.4)
        task.cancel()

    time.sleep(0.01)
    asyncio.run(beats())
    assert lease_row(api)[0].heartbeat_at > before and not engine.lost


def test_a_listener_that_finds_its_place_taken_stops_taking_tasks(api):
    import asyncio
    from store import db
    from store.models import TesListener
    engine = api.app.state.engine
    created = api.client.post("/v1/tasks", headers=alice(), json=task(
        api, ["sleep", "30"]))
    task_id = created.json()["id"]
    for _ in range(100):
        if api.client.get(f"/v1/tasks/{task_id}", headers=alice()).json()["state"] == "RUNNING":
            break
        time.sleep(0.1)
    with db.writing(db.make_engine(api.values["BIOCHEF_DB_URL"])) as session:
        row = session.get(TesListener, 1)
        row.holder = "another-listener"
        session.add(row)
    asyncio.run(asyncio.wait_for(engine.heartbeat(interval=0.05), timeout=10))
    assert engine.lost
    assert api.events("tes.lock.lost")[0]["reason"] == "another listener holds the database"
    answer = api.client.post("/v1/tasks", headers=alice(), json=task(api, ["true"]))
    assert answer.status_code == 503 and "lost its place" in answer.json()["msg"]
    assert wait(api, task_id)["state"] == "CANCELED", "and what it was running was ended"


def test_a_database_that_cannot_be_reached_for_most_of_the_lease_loses_the_place(api, monkeypatch):
    import asyncio
    from tesapi import records
    def unreachable(bind, holder, now=None):
        raise records.RecordError("the lease could not be renewed: OperationalError")
    monkeypatch.setattr(records, "beat", unreachable)
    monkeypatch.setattr(records, "LEASE_S", 0.3)
    engine = api.app.state.engine
    asyncio.run(asyncio.wait_for(engine.heartbeat(interval=0.05), timeout=10))
    assert engine.lost
    assert "could not be reached" in api.events("tes.lock.lost")[0]["reason"]


def test_only_the_holder_can_give_the_place_up(world):
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    records.acquire(engine, "one")
    records.release(engine, "someone-else")
    assert records.current(engine) is not None, "another listener's release changed nothing"
    records.release(engine, "one")
    assert records.current(engine) is None


def test_a_failure_to_renew_now_and_then_does_not_lose_the_place(api, monkeypatch):
    import asyncio
    from tesapi import records
    real, failures = records.beat, {"left": 2}

    def flaky(bind, holder, now=None):
        if failures["left"]:
            failures["left"] -= 1
            raise records.RecordError("the lease could not be renewed: OperationalError")
        return real(bind, holder, now)

    async def run():
        task = asyncio.create_task(api.app.state.engine.heartbeat(interval=0.05))
        await asyncio.sleep(0.6)
        task.cancel()

    monkeypatch.setattr(records, "beat", flaky)
    monkeypatch.setattr(records, "LEASE_S", 0.5)
    asyncio.run(run())
    assert failures["left"] == 0 and not api.app.state.engine.lost


def test_a_listener_that_keeps_renewing_keeps_its_place_past_the_lease(api, monkeypatch):
    import asyncio
    from tesapi import records

    async def run():
        task = asyncio.create_task(api.app.state.engine.heartbeat(interval=0.05))
        await asyncio.sleep(0.8)
        task.cancel()

    monkeypatch.setattr(records, "LEASE_S", 0.3)
    asyncio.run(run())
    assert not api.app.state.engine.lost


def test_the_running_listener_starts_its_heartbeat(world, monkeypatch):
    from tesapi import records
    monkeypatch.setattr(records, "BEAT_S", 0.05)
    with TestClient(create_app(world.config(), RecordingRunner())):
        first = lease_row(world)[0].heartbeat_at
        time.sleep(0.5)
        assert lease_row(world)[0].heartbeat_at > first


@pytest.mark.parametrize("name", ["BIOCHEF_TES_TOKEN_FILE", "BIOCHEF_TES_PASSWORD_FILE"])
def test_the_runners_credential_files_are_places_no_task_may_use(world, name):
    """Set on its own, in a directory of its own, so no other setting hides a miss."""
    place = world.tmp / "runner-credentials"
    place.mkdir()
    (place / "file").write_text("synthetic-credential\n")
    values = {k: v for k, v in world.values.items()
              if k not in ("BIOCHEF_TESAPI_TOKENS", "BIOCHEF_LOCAL_ROOT", "BIOCHEF_RUN_ROOT")}
    values.update({name: str(place / "file"), "BIOCHEF_LOCAL_ROOT": str(world.data),
                   "BIOCHEF_RUN_ROOT": str(world.runs)})
    values["BIOCHEF_TESAPI_TOKENS"] = world.values["BIOCHEF_TESAPI_TOKENS"]
    with pytest.raises(settings.ConfigError, match=f"overlaps .*\\({name}\\)"):
        settings.load({**values, "BIOCHEF_TESAPI_READ_DIRS": str(place.resolve())})
    with pytest.raises(settings.ConfigError, match=f"overlaps .*\\({name}\\)"):
        settings.load({**values, "BIOCHEF_TESAPI_WRITE_DIRS": str(place.resolve())})


def test_a_lost_race_for_the_place_is_a_refusal_not_a_crash(world, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy.exc import IntegrityError
    from store import db
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    real = db.writing

    @contextmanager
    def second_row(bind):
        with real(bind):
            raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed"))
        yield

    monkeypatch.setattr(db, "writing", second_row)
    with pytest.raises(records.Held, match="same moment"):
        records.acquire(engine, "late")


def test_the_place_is_taken_only_if_it_is_as_it_was_read(world, monkeypatch):
    """Another listener renews the place between the read and the write."""
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from store import db
    from store.models import TesListener
    from tesapi import records
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    old = datetime.now(timezone.utc) - timedelta(seconds=records.LEASE_S + 60)
    records.acquire(engine, "dead", now=old)
    real = db.writing

    @contextmanager
    def interfering(bind):
        with real(bind) as session:
            class Session:
                def __getattr__(self, name):
                    return getattr(session, name)

                def get(self, *args):
                    row = session.get(*args)
                    session.execute(update(TesListener).where(TesListener.id == 1)
                                    .values(heartbeat_at=row.heartbeat_at + timedelta(seconds=1))
                                    .execution_options(synchronize_session=False))
                    return row
            yield Session()

    monkeypatch.setattr(db, "writing", interfering)
    with pytest.raises(records.Held, match="same moment"):
        records.acquire(engine, "me")


def test_a_listener_that_lost_its_place_writes_nothing_more(api, monkeypatch):
    from tesapi import records, tasks as task_module
    engine = api.app.state.engine
    engine.lost = True
    monkeypatch.setattr(records, "save", lambda *a: pytest.fail("wrote after losing the place"))
    live = task_module.Task("alice", task_module.parse(task(api, ["true"]),
                                                       api.app.state.policy))
    engine._keep(live)


def test_a_good_renewal_resets_the_clock_for_the_next_failure(api, monkeypatch):
    import asyncio
    from tesapi import records
    real, calls = records.beat, {"n": 0}

    def late_trouble(bind, holder, now=None):
        calls["n"] += 1
        if 13 <= calls["n"] <= 14:
            raise records.RecordError("the lease could not be renewed: OperationalError")
        return real(bind, holder, now)

    async def run():
        task = asyncio.create_task(api.app.state.engine.heartbeat(interval=0.05))
        await asyncio.sleep(1.0)
        task.cancel()

    monkeypatch.setattr(records, "beat", late_trouble)
    monkeypatch.setattr(records, "LEASE_S", 0.5)
    asyncio.run(run())
    assert calls["n"] > 14 and not api.app.state.engine.lost


def test_the_database_holds_at_most_one_lease(world):
    from sqlalchemy.exc import IntegrityError
    from datetime import datetime, timezone
    from store import db
    from store.models import TesListener
    engine = db.make_engine(world.values["BIOCHEF_DB_URL"])
    now = datetime.now(timezone.utc)
    with pytest.raises(IntegrityError):
        with db.writing(engine) as session:
            session.add(TesListener(id=2, holder="x", host="h", pid=1,
                                    started_at=now, heartbeat_at=now))


def test_a_listener_started_against_a_held_database_is_refused_with_a_message(
        api, capsys, monkeypatch):
    from tesapi.__main__ import main
    for key, value in api.values.items():
        monkeypatch.setenv(key, value)
    import tesapi.__main__ as entry
    monkeypatch.setattr(entry, "get_runner", lambda name: RecordingRunner(network=ATTESTED))
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    assert main([]) == 1
    assert "another TES listener holds this database" in capsys.readouterr().err


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
            "bad = [m for m in ('main', 'ui', 'node', 'runs') "
            "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_listener_opens_two_tables_of_the_database_and_never_the_other_apps():
    sources = {source.name: source.read_text()
               for source in (REPO_ROOT / "tesapi").glob("*.py")}
    for name, text in sources.items():
        # Not the agent's API, the page, the node's logic or the federated run store.
        assert not re.search(r"^\s*(from|import)\s+(node|ui|main|runs)\b", text, re.M), name
        # The database is opened in records.py. Elsewhere only the engine is made
        # (app.py, __main__.py) and the default URL is read (config.py).
        if name != "records.py":
            assert not re.search(r"sqlmodel|sqlalchemy|store\.models|store\.repo", text), name
    records_text = sources["records.py"]
    models = re.findall(r"from store\.models import ([^\n]+)", records_text)
    assert models == ["TesListener, TesTask"], \
        "records.py reads and writes tes_task and tes_listener and no other table"
    assert not re.search(r"store\.repo|RunSummary|Study|Request|Decision|DatasetBinding",
                         records_text)


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
