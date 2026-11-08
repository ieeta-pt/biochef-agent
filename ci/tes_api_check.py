"""The TES API in front of rootless podman, run for real, as a user that is not root.

SPEC.md, section 14.4. A listener is started in this process on a loopback port, with
the podman runner, and tasks are sent to it over HTTP. From inside the steps and from
the files it checks what the listener promises:

  - the task ran in the image by digest, not on the host
  - the agent's environment, another directory of the host and the datasets are not
    there, and the step has no network
  - the output is where the task asked, owned by the agent
  - a request with no token, an image that is not listed, a URL that is not file://,
    a path outside the listed directories and a dataset path are all refused
  - the audit trail has the events, each with "source": "tes", and no secret in it

The runner is podman, so the guarantees are the agent's own: network, filesystem and
environment enforced. It is not as root.
"""

import json
import os
import secrets
import socket
import sys
import tempfile
from datetime import datetime, timezone
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import uvicorn  # noqa: E402

from store import db, migrate  # noqa: E402

from runner import PodmanRunner  # noqa: E402
from tesapi import config as settings  # noqa: E402
from tesapi.__main__ import prepare, start_checks  # noqa: E402
from tesapi.app import create_app  # noqa: E402

IMAGE = os.environ["BIOCHEF_TESAPI_IMAGE"]
SECRET = "agent-only-secret-value-7731"


def check(condition, message):
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok: {message}")


def call(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def finish(base, token, task_id):
    for _ in range(600):
        status, body = call(base, "GET", f"/v1/tasks/{task_id}?view=FULL", token=token)
        if body["state"] in ("COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED"):
            return body
        time.sleep(0.5)
    check(False, "the task finished")


def main():
    if os.geteuid() == 0:
        print("FAIL: run this as a user that is not root", file=sys.stderr)
        return 2
    os.environ["BIOCHEF_PARITY_ENV_PROBE"] = SECRET
    root = Path(tempfile.mkdtemp(prefix="biochef-tesapi-"))
    exchange, datasets, runs, keys = (root / n for n in ("exchange", "datasets", "runs", "keys"))
    outbox = root / "outbox"
    for folder in (exchange, outbox, datasets / "cohort", runs, keys):
        folder.mkdir(parents=True)
    (datasets / "cohort" / "patients.txt").write_text("synthetic patient 1\n")
    (exchange / "in.txt").write_text("alpha\n")
    token = secrets.token_urlsafe(24)
    tokens = keys / "tokens"
    tokens.write_text(f"ci sha256:{settings.hash_token(token)}\n")
    tokens.chmod(0o600)
    trail = root / "audit.jsonl"
    os.environ["BIOCHEF_AUDIT_LOG"] = str(trail)
    os.environ["BIOCHEF_REQUIRE_NETWORK_ISOLATION"] = "true"

    config = settings.load({
        "BIOCHEF_AUDIT_LOG": str(trail), "BIOCHEF_TESAPI_TOKENS": str(tokens),
        "BIOCHEF_TESAPI_IMAGES": IMAGE, "BIOCHEF_TES_READ_DIRS": str(exchange.resolve()),
        "BIOCHEF_TES_WRITE_DIRS": str(outbox.resolve()),
        "BIOCHEF_DB_URL": f"sqlite:///{root}/agent.db",
        "BIOCHEF_LOCAL_ROOT": str(datasets), "BIOCHEF_RUN_ROOT": str(runs),
        "BIOCHEF_TESAPI_TIMEOUT": "300"})
    provider = PodmanRunner(image=f"docker://{IMAGE}", selinux="disable", mode="rootless",
                            pids="256", memory="1g")
    print("runner:", provider.describe(), provider.guarantees)
    prepare(config, provider)
    migrate.upgrade(config.db_url)
    bind = db.make_engine(config.db_url)

    def serve(app):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="warning", proxy_headers=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(300):
            if server.started:
                break
            time.sleep(0.1)
        return server, thread, f"http://127.0.0.1:{port}"

    server, thread, base = serve(create_app(config, provider,
                                            start_checks(config, provider), bind))
    check(server.started, "the listener started, after the network check passed through podman")

    check(call(base, "GET", "/v1/service-info")[0] == 401, "no token, no answer")
    check(call(base, "GET", "/v1/service-info", token="wrong-token-123456")[0] == 401,
          "a wrong token, no answer")

    def task(command, **more):
        body = {"name": "ci", "executors": [{"image": IMAGE, "command": command}]}
        body.update(more)
        return body

    script = (
        "( . /etc/os-release; echo os=$ID ) > out.txt; "
        "echo user=$(id -u) >> out.txt; "
        "if [ -e " + str(datasets / "cohort" / "patients.txt") + " ]; then echo data=visible; "
        "else echo data=hidden; fi >> out.txt; "
        "if env | grep -q " + SECRET + "; then echo env=inherited; else echo env=clean; fi >> out.txt; "
        "routes=$(grep -vc '^Iface' /proc/net/route); echo routes=$routes >> out.txt; "
        "tr a-z A-Z < in.txt >> out.txt")
    status, created = call(base, "POST", "/v1/tasks", token=token, body=task(
        ["bash", "-c", script],
        inputs=[{"url": "file://" + str(exchange / "in.txt"), "path": "/work/in.txt"}],
        outputs=[{"url": "file://" + str(outbox / "out.txt"), "path": "/work/out.txt"}]))
    check(status == 200, f"a task was accepted ({status} {created})")
    done = finish(base, token, created["id"])
    check(done["state"] == "COMPLETE", f"the task completed ({done['state']} "
          f"{done['logs'][0]['system_logs']} {done['logs'][0]['logs']})")
    lines = (outbox / "out.txt").read_text().split()
    result = dict(line.split("=", 1) for line in lines if "=" in line)
    host_os = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines()
                   if "=" in l)["ID"]
    check(result["os"] != host_os, f"the task ran in the image ({result['os']}), not on the host ({host_os})")
    check(result["data"] == "hidden", "the datasets are not there")
    check(result["env"] == "clean", "the agent's environment is not there")
    check(result["routes"] == "0", "the step had no network route")
    check("ALPHA" in lines, "it read its input and wrote its output")
    check((outbox / "out.txt").stat().st_uid == os.getuid(), "the output is the agent's")
    check(not list(runs.iterdir()), "the workspace was removed")

    refusals = [
        ("an image that is not listed", task(["true"], executors=[
            {"image": IMAGE.replace("debian", "ubuntu"), "command": ["true"]}]), 403),
        ("a URL that is not file://", task(["true"], inputs=[
            {"url": "https://example.org/x", "path": "/work/x"}]), 403),
        ("a path outside the listed directories", task(["true"], inputs=[
            {"url": "file:///etc/hostname", "path": "/work/x"}]), 403),
        ("a dataset", task(["true"], inputs=[
            {"url": "file://" + str(datasets / "cohort" / "patients.txt"),
             "path": "/work/x"}]), 403),
        ("an input from a directory it may only write to", task(["true"], inputs=[
            {"url": "file://" + str(outbox / "out.txt"), "path": "/work/x"}]), 403),
        ("an output into a directory it may only read from", task(["true"], outputs=[
            {"url": "file://" + str(exchange / "back.txt"), "path": "/work/x"}]), 403),
        ("an env", task(["true"], executors=[
            {"image": IMAGE, "command": ["true"], "env": {"K": "V"}}]), 400),
    ]
    for label, body, expected in refusals:
        status, answer = call(base, "POST", "/v1/tasks", token=token, body=body)
        check(status == expected, f"{label} is refused ({status}: {answer.get('msg', '')[:70]})")

    events = [json.loads(line) for line in trail.read_text().splitlines()]
    mine = [e for e in events if e["event"].startswith(("tes.", "node.runner"))]
    check(all(e.get("source") == "tes" for e in mine), "every event has source tes")
    kinds = {e["event"] for e in mine}
    check({"tes.start", "tes.task.created", "tes.task.state", "tes.task.refused",
           "tes.auth.refused", "node.runner.network_check"} <= kinds,
          f"the trail has the events ({sorted(kinds)})")
    text = trail.read_text()
    check(token not in text and SECRET not in text, "no token and no secret in the trail")
    server.should_exit = True
    thread.join(timeout=30)

    # A restart: the listener is started again on the same database. The task that
    # finished is there; one that was running when the first listener stopped is not
    # still running, and says so.
    from store.models import TesTask
    with db.writing(bind) as session:
        session.add(TesTask(id="lost" + "0" * 28, owner="ci", name="ci", image=IMAGE,
                            command=["sleep", "999"], state="RUNNING",
                            created_at=datetime.now(timezone.utc)))
    server, thread, base = serve(create_app(config, provider, None, bind))
    check(server.started, "the listener started again on the same database")
    status, again = call(base, "GET", f"/v1/tasks/{created['id']}?view=FULL", token=token)
    check(status == 200 and again["state"] == "COMPLETE",
          f"the task that finished is still there after a restart ({status})")
    status, lost = call(base, "GET", f"/v1/tasks/{'lost' + '0' * 28}?view=FULL", token=token)
    check(status == 200 and lost["state"] == "SYSTEM_ERROR"
          and "stopped" in " ".join(lost["logs"][0]["system_logs"]),
          f"a task that was running when it stopped is SYSTEM_ERROR ({lost.get('state')})")
    server.should_exit = True
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
