"""Whether this agent can say it is alive, and how busy it is (#82).

Recorded before anything changes. Nothing does.

A hub orchestrating several sites needs two answers and they are different
questions. A liveness probe is run by an orchestrator, not a person, and carries
no credentials -- so if liveness needs a token, a misconfigured token makes a
healthy service look dead and get restarted forever. Capacity, on the other
hand, describes the deployment, and free slots and runner configuration are
operational detail.

Today there is neither. The only way the hub can discover that this agent is
saturated is to submit work and be refused with 503, which is finding out by
having already sent it to the wrong site.

The tests here are expected to be REPLACED by the change that closes #82, so
that commit has to delete a statement that this service cannot answer rather
than quietly adding an endpoint nobody probed.
"""

import re
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras_mod = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


def _source(name):
    """Read rather than imported, for the reason the other modules here give:
    each installs stubs in sys.modules, so what an import finds in a full run
    depends on which test module got there first."""
    return (REPO_ROOT / name).read_text()


def test_the_scan_finds_the_modules_it_claims_to_read():
    """Guard against every assertion below passing over nothing."""
    assert "@app.post(\"/runs\"" in _source("main.py")
    assert "class AuthenticationMiddleware" in _source("auth.py")


def test_no_endpoint_reports_liveness():
    """The gap, as an assertion, so it cannot be argued about."""
    routes = re.findall(r'@app\.(?:get|post|delete)\("([^"]+)"\)', _source("main.py"))
    assert routes, "the route scan matched nothing"
    for probe in ("/health", "/healthz", "/livez", "/readyz", "/ping"):
        assert probe not in routes, (
            f"{probe} exists; if liveness has landed, this test should be "
            f"replaced by one asserting it answers without credentials"
        )


def test_no_endpoint_reports_capacity():
    routes = re.findall(r'@app\.(?:get|post|delete)\("([^"]+)"\)', _source("main.py"))
    for described in ("/capacity", "/service-info", "/describe", "/metrics"):
        assert described not in routes, f"{described} exists"


def test_every_request_is_authenticated_without_exception():
    """There is no unauthenticated path today, so there is no exemption surface
    to get wrong -- yet.

    When one arrives it is the whole risk of that change, and it should be a set
    small enough to read at a glance and pinned by a test, because anything in
    it answers to whatever can reach the port.
    """
    code = "\n".join(l.split("#", 1)[0] for l in _source("auth.py").splitlines())
    middleware = code.split("class AuthenticationMiddleware")[1]
    for escape in ("OPEN", "EXEMPT", "ALLOW", "PUBLIC", "skip"):
        assert escape not in middleware, (
            f"AuthenticationMiddleware now has {escape!r}; an unauthenticated "
            f"path needs its own tests, not this one"
        )


def test_the_run_store_cannot_count_what_it_holds():
    """A capacity answer needs this, and nothing provides it.

    Counting has to come from the store rather than a running tally: a tally
    drifts the first time an eviction or a refused transition is not accounted
    for, and a count that is quietly wrong is worse than one that costs a walk.
    """
    code = _source("runs.py")
    for counter in ("def state_counts", "def retained"):
        assert counter not in code, f"{counter} exists in runs.py"


def test_the_hub_can_only_learn_saturation_by_being_refused():
    """Which is finding out after sending work to the wrong site."""
    code = _source("main.py")
    assert "RunCapacityError" in code
    assert "503" in code
    assert "Retry-After" in code
