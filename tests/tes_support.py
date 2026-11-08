"""A small GA4GH TES server, for the tests and for CI.

It speaks the part of TES 1.1 the agent uses: service-info, create, get, list and
cancel, with file:// inputs and outputs. A task runs its first executor either as
a subprocess in a directory of its own (the unit tests, where there is no
container), or in a docker container with no network (CI, where there is one), and
in both the wrapper tes.py sends is the real one, so the hashing, the manifest and
the diff of the workspace are what runs.

It is not Funnel. What it stands in for is the shape of the exchange: a server that
copies a file:// directory in, runs the command, and copies a file:// directory
out, as whoever the server is. Run as root, as CI does, it returns files owned by
root, which is the case tes-step-root exists for.

`tamper(out_dir)` is called after the command and before the outputs are uploaded,
so a test can corrupt, add or replace a file the way a bad server could.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TERMINAL = {"COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED"}


def _path(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "file":
        raise ValueError(f"not a file:// URL: {url}")
    return urllib.parse.unquote(parts.path)


class TesServer:
    def __init__(self, allowed, mode="subprocess", token=None, tamper=None,
                 on_task=None):
        self.allowed = [os.path.realpath(each) for each in allowed]
        self.mode = mode
        self.token = token
        self.tamper = tamper
        self.on_task = on_task
        self.tasks = {}
        self.posted = []
        self.seen_headers = []
        self.cancelled = []
        self.scratch = tempfile.mkdtemp(prefix="tes-server-")
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self):
                server.seen_headers.append(dict(self.headers))
                if server.token is None:
                    return True
                if self.headers.get("Authorization") == f"Bearer {server.token}":
                    return True
                self._send(401, {"message": "unauthorised"})
                return False

            def do_GET(self):
                if not self._authorised():
                    return
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path == "/v1/service-info":
                    return self._send(200, {
                        "id": "org.example.tes-stand-in", "name": "stand-in",
                        "version": "1.1.0",
                        "type": {"group": "org.ga4gh", "artifact": "tes",
                                 "version": "1.1.0"}})
                if parts.path == "/v1/tasks":
                    prefix = (query.get("name_prefix") or [""])[0]
                    found = [{"id": key, "state": record["state"]}
                             for key, record in list(server.tasks.items())
                             if record["task"].get("name", "").startswith(prefix)]
                    return self._send(200, {"tasks": found, "next_page_token": ""})
                if parts.path.startswith("/v1/tasks/"):
                    record = server.tasks.get(parts.path[len("/v1/tasks/"):])
                    if record is None:
                        return self._send(404, {"message": "no such task"})
                    return self._send(200, server._view(record))
                self._send(404, {"message": "no such route"})

            def do_POST(self):
                if not self._authorised():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path == "/v1/tasks":
                    task = json.loads(body)
                    server.posted.append(task)
                    task_id = uuid.uuid4().hex[:12]
                    record = {"id": task_id, "task": task, "state": "QUEUED",
                              "logs": [], "system_logs": [],
                              "cancel": threading.Event(), "process": None}
                    server.tasks[task_id] = record
                    threading.Thread(target=server._run, args=(record,),
                                     daemon=True).start()
                    return self._send(200, {"id": task_id})
                if self.path.startswith("/v1/tasks/") and self.path.endswith(":cancel"):
                    task_id = self.path[len("/v1/tasks/"):-len(":cancel")]
                    record = server.tasks.get(task_id)
                    if record is None:
                        return self._send(404, {"message": "no such task"})
                    server.cancelled.append(task_id)
                    record["cancel"].set()
                    process = record["process"]
                    if process is not None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    return self._send(200, {})
                self._send(404, {"message": "no such route"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.scratch, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _view(self, record):
        view = dict(record["task"])
        view.update(id=record["id"], state=record["state"],
                    logs=[{"logs": record["logs"], "system_logs": record["system_logs"],
                           "outputs": []}])
        return view

    def _inside(self, path):
        real = os.path.realpath(path)
        return any(real == root or real.startswith(root + os.sep)
                   for root in self.allowed)

    def _run(self, record):
        task = record["task"]
        base = os.path.join(self.scratch, record["id"])
        work, out = os.path.join(base, "work"), os.path.join(base, "out")
        try:
            record["state"] = "INITIALIZING"
            os.makedirs(work)
            os.makedirs(out)
            volumes = {}
            for item in task.get("inputs", []):
                source = _path(item["url"])
                if not self._inside(source):
                    raise PermissionError(f"{source} is not in an allowed directory")
                shutil.copytree(source, work, symlinks=True, dirs_exist_ok=True)
                volumes[item["path"]] = work
            executor = task["executors"][0]
            if self.on_task:
                self.on_task(task)
            record["state"] = "RUNNING"
            code, stdout, stderr = self._execute(record, executor, work, out)
            if record["cancel"].is_set():
                record["state"] = "CANCELED"
                return
            record["logs"].append({"exit_code": code, "stdout": stdout,
                                   "stderr": stderr})
            if code != 0:
                record["state"] = "EXECUTOR_ERROR"
                return
            if self.tamper:
                self.tamper(out)
            for item in task.get("outputs", []):
                target = _path(item["url"])
                if not self._inside(target):
                    raise PermissionError(f"{target} is not in an allowed directory")
                shutil.copytree(out, target, symlinks=True, dirs_exist_ok=True)
            record["state"] = "COMPLETE"
        except Exception as error:      # noqa: BLE001
            record["system_logs"].append(f"{type(error).__name__}: {error}")
            record["state"] = "SYSTEM_ERROR"

    def _execute(self, record, executor, work, out):
        command = list(executor["command"])
        if self.mode == "subprocess":
            # No container: /work and /out are the two directories, named here.
            if len(command) > 2:
                command[2] = (command[2].replace("/in/.", work + "/.")
                                         .replace("/out", out))
            argv, kwargs = command, {"cwd": work, "env": {
                "PATH": os.environ["PATH"], "HOME": work}}
        else:
            name = f"tes-{record['id']}"
            record["container"] = name
            argv = ["docker", "run", "--rm", "--name", name, "--network", "none",
                    "--read-only", "--tmpfs", "/tmp",
                    "-v", f"{work}:/in:ro", "-v", f"{out}:/out",
                    *(["-w", executor["workdir"]] if executor.get("workdir") else []),
                    executor["image"], *command]
            kwargs = {}
        process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, **kwargs)
        record["process"] = process
        if record["cancel"].is_set():
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        if self.mode != "subprocess" and record["cancel"].is_set():
            subprocess.run(["docker", "kill", record["container"]],
                           capture_output=True, check=False)
        return process.returncode, stdout, stderr


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description="a TES server for CI")
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--mode", default="docker")
    parser.add_argument("--token-file")
    parser.add_argument("--ready-file", required=True)
    arguments = parser.parse_args()
    secret = None
    if arguments.token_file:
        with open(arguments.token_file) as stream:
            secret = stream.read().strip()
    server = TesServer(arguments.allow, mode=arguments.mode, token=secret).start()
    with open(arguments.ready_file, "w") as stream:
        stream.write(server.url)
    signal.signal(signal.SIGTERM, lambda *args: (server.stop(), os._exit(0)))
    while True:
        time.sleep(1)
