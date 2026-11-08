"""What each runner guarantees, the rule that reads it, and the check at start.

SPEC.md, section 14.1: each runner says whether the agent keeps a step off the
network, whether it sees only its workspace and image, and whether it inherits
the agent's environment, as enforced, attested or none.
BIOCHEF_REQUIRE_NETWORK_ISOLATION refuses anything but an enforced network, and
a runner that claims one shows it with a step at start.
"""

import json
import os
import socket
import sys
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

import runner
from runner import (ATTESTED, ENFORCED, NONE, PROVIDERS, ApptainerRunner,
                    PodmanRunner, Runner, SubprocessRunner, check_network,
                    get_runner, network_probe, require_network_isolation)


@pytest.fixture(autouse=True)
def no_rule(monkeypatch):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "false")
    monkeypatch.delenv("BIOCHEF_NETWORK_PROBE", raising=False)
    monkeypatch.setenv("BIOCHEF_PODMAN_MODE", "rootless")
    monkeypatch.setenv("BIOCHEF_PODMAN_SELINUX", "disable")
    monkeypatch.setenv("BIOCHEF_STEP_PIDS", "64")
    monkeypatch.setenv("BIOCHEF_STEP_MEMORY", "1g")


# --------------------------------------------------------------------------
# what each runner says


def test_subprocess_guarantees_nothing():
    assert SubprocessRunner().guarantees == {
        "network": NONE, "filesystem": NONE, "environment": NONE}
    assert SubprocessRunner().isolates_network is False


def test_apptainer_by_default_enforces_all_three():
    assert ApptainerRunner().guarantees == {
        "network": ENFORCED, "filesystem": ENFORCED, "environment": ENFORCED}


@pytest.mark.parametrize("args,expected", [
    ("", (NONE, NONE, NONE)),
    ("--contain", (NONE, ENFORCED, NONE)),
    ("--cleanenv", (NONE, NONE, ENFORCED)),
    ("--net --network none", (ENFORCED, NONE, NONE)),
    ("--containall --net --network none", (ENFORCED, ENFORCED, ENFORCED)),
    ("--contain --cleanenv --net --network bridge", (NONE, ENFORCED, ENFORCED)),
])
def test_apptainer_reads_the_flags_actually_passed(args, expected):
    found = ApptainerRunner(apptainer_args=args).guarantees
    assert (found["network"], found["filesystem"], found["environment"]) == expected


def test_podman_enforces_all_three():
    assert set(PodmanRunner().guarantees.values()) == {ENFORCED}


class Attested(SubprocessRunner):
    name = "attested"

    @property
    def guarantees(self):
        return {"network": ATTESTED, "filesystem": ATTESTED, "environment": ATTESTED}


def test_an_attested_network_is_not_isolation():
    assert Attested().isolates_network is False


# --------------------------------------------------------------------------
# BIOCHEF_REQUIRE_NETWORK_ISOLATION


@pytest.mark.parametrize("value,expected", [("", True), ("false", False),
                                            ("true", True), (" true ", True),
                                            (" false ", False)])
def test_the_rule_is_true_or_false(value, expected):
    assert require_network_isolation(value) is expected


@pytest.mark.parametrize("value", ["yes", "TRUE", "1", "on", "False"])
def test_anything_else_stops_the_agent(value):
    with pytest.raises(ValueError, match="must be true or false"):
        require_network_isolation(value)


def test_turned_off_any_runner_starts():
    assert isinstance(get_runner("subprocess"), SubprocessRunner)


def test_the_rule_is_on_when_unset(monkeypatch):
    monkeypatch.delenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION")
    assert require_network_isolation() is True
    with pytest.raises(ValueError, match="refuses BIOCHEF_RUNNER=subprocess"):
        get_runner("subprocess")
    assert get_runner("apptainer").isolates_network


def test_turning_it_off_is_audited_at_start(api, monkeypatch):
    main, trail = api
    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    main.record_isolation_choice()
    event = json.loads(trail.read_text())
    assert event["event"] == "node.runner.isolation_not_required"
    assert event["runner"] == "subprocess" and event["network"] == "none"
    assert event["setting"] == "BIOCHEF_REQUIRE_NETWORK_ISOLATION=false"


@pytest.mark.parametrize("value", [None, "true"])
def test_with_the_rule_on_nothing_is_audited(api, monkeypatch, value):
    main, trail = api
    if value is None:
        monkeypatch.delenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION")
    else:
        monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", value)
    assert main.record_isolation_choice() is None
    assert not trail.exists()


def test_the_rule_refuses_subprocess(monkeypatch):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    with pytest.raises(ValueError, match="refuses BIOCHEF_RUNNER=subprocess.*is none"):
        get_runner("subprocess")


def test_the_rule_refuses_apptainer_without_its_flags(monkeypatch):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    monkeypatch.setattr(runner, "APPTAINER_ARGS", "--contain --cleanenv")
    with pytest.raises(ValueError, match="refuses BIOCHEF_RUNNER=apptainer"):
        get_runner("apptainer")


def test_the_rule_refuses_an_attested_network(monkeypatch):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    monkeypatch.setitem(PROVIDERS, "attested", Attested)
    with pytest.raises(ValueError, match="is attested"):
        get_runner("attested")


@pytest.mark.parametrize("name", ["apptainer", "podman"])
def test_the_rule_accepts_an_enforced_network(monkeypatch, name):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    assert get_runner(name).isolates_network


def test_a_bad_rule_value_stops_get_runner(monkeypatch):
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "yes")
    with pytest.raises(ValueError, match="must be true or false"):
        get_runner("apptainer")


# --------------------------------------------------------------------------
# BIOCHEF_NETWORK_PROBE


@pytest.mark.parametrize("value,expected", [
    ("", None), ("example.org:443", ("example.org", 443)),
    ("10.0.0.1:53", ("10.0.0.1", 53)), ("coordinator.tre.local:8443",
                                        ("coordinator.tre.local", 8443)),
])
def test_a_probe_is_host_and_port(value, expected):
    assert network_probe(value) == expected


@pytest.mark.parametrize("value", ["example.org", "example.org:0", "example.org:70000",
                                   "a;b:1", "a b:1", "$(id):1", "-x:1", "[::1]:53"])
def test_anything_else_is_not_a_probe(value):
    with pytest.raises(ValueError, match="must be host:port"):
        network_probe(value)


# --------------------------------------------------------------------------
# the check at start


class Scripted(Runner):
    """Enforces the network, and its "step" writes what a test says it saw."""

    name = "scripted"

    def __init__(self, seen, code=0, keep=None):
        self.seen, self.code, self.keep = seen, code, keep

    @property
    def guarantees(self):
        return {"network": ENFORCED, "filesystem": ENFORCED, "environment": ENFORCED}

    def command(self, ws):
        copy = f"cp Snakefile {self.keep}; " if self.keep else ""
        return ["sh", "-c", f"{copy}printf '{self.seen}' > network-check.txt; "
                            f"exit {self.code}"]


@pytest.fixture
def listening():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    yield server.getsockname()
    server.close()


def test_a_runner_that_claims_nothing_is_not_checked():
    assert check_network(SubprocessRunner()) is None


def test_no_interface_but_loopback_passes(tmp_path):
    result = check_network(Scripted("0 1\n"), root=str(tmp_path))
    assert result.ok and "no BIOCHEF_NETWORK_PROBE" in result.detail
    assert list(tmp_path.iterdir()) == [], "the check leaves nothing behind"


def test_an_interface_up_fails(tmp_path):
    result = check_network(Scripted("2 1\n"), root=str(tmp_path))
    assert not result.ok and "saw 2 routes or addresses" in result.detail


def test_a_step_that_cannot_read_its_routes_fails(tmp_path):
    result = check_network(Scripted("0 0\n"), root=str(tmp_path))
    assert not result.ok and "could not read its network routes" in result.detail


def test_a_step_that_did_not_run_fails(tmp_path):
    result = check_network(Scripted("0 1\n", code=3), root=str(tmp_path))
    assert not result.ok and "did not run (exit 3)" in result.detail


def test_an_unreachable_probe_passes(tmp_path, listening):
    result = check_network(Scripted("0 1\nunreachable\n"), root=str(tmp_path),
                           probe=listening)
    assert result.ok and f"{listening[0]}:{listening[1]} unreachable" in result.detail


def test_a_reached_probe_fails(tmp_path, listening):
    result = check_network(Scripted("0 1\nreachable\n"), root=str(tmp_path),
                           probe=listening)
    assert not result.ok and "a step reached" in result.detail


def test_a_probe_the_agent_cannot_reach_proves_nothing(tmp_path):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    address = closed.getsockname()
    closed.close()
    result = check_network(Scripted("0 1\nunreachable\n"), root=str(tmp_path),
                           probe=address)
    assert not result.ok and "the agent itself cannot reach" in result.detail


def test_the_step_tries_the_probe(tmp_path, listening):
    kept = tmp_path / "kept"
    root = tmp_path / "runs"
    root.mkdir()
    check_network(Scripted("0 1\nunreachable\n", keep=kept), root=str(root),
                  probe=listening)
    text = kept.read_text()
    assert f"/dev/tcp/{listening[0]}/{listening[1]}" in text
    assert "/proc/net/route" in text and "/proc/net/if_inet6" in text


def test_the_probe_comes_from_the_setting(tmp_path, listening, monkeypatch):
    monkeypatch.setenv("BIOCHEF_NETWORK_PROBE", f"{listening[0]}:{listening[1]}")
    result = check_network(Scripted("0 1\nunreachable\n"), root=str(tmp_path))
    assert result.ok and str(listening[1]) in result.detail


def test_the_host_itself_fails_the_check(tmp_path):
    """The real step, run on the host: a check that passed here would be useless."""
    result = check_network(SubprocessRunner(), root=str(tmp_path), force=True)
    assert result is not None and not result.ok
    # Linux has the host's routes; a host without /proc/net, such as macOS,
    # cannot read them, which fails as well.
    assert ("routes or addresses" in result.detail) == sys.platform.startswith("linux")


@pytest.fixture
def net(tmp_path, monkeypatch):
    """The real rule, run by snakemake, against routes and addresses of a test's own."""
    folder = tmp_path / "net"
    folder.mkdir()
    monkeypatch.setattr(runner, "NET", str(folder))

    def write(routes=(), addresses=()):
        (folder / "route").write_text(
            "Iface\tDestination\tGateway\n"
            + "".join(f"{dev}\t00000000\t00000000\n" for dev in routes))
        (folder / "if_inet6").write_text("".join(
            f"00000000000000000000000000000001 01 80 10 80 {dev}\n" for dev in addresses))
    return write


class Host(SubprocessRunner):
    @property
    def guarantees(self):
        return {"network": ENFORCED, "filesystem": NONE, "environment": NONE}


@pytest.mark.parametrize("routes,addresses,ok", [
    ((), ("lo",), True),
    (("lo",), ("lo",), True),
    (("eth0",), ("lo",), False),
    ((), ("lo", "eth0"), False),
])
def test_the_real_rule_counts_routes_and_addresses(net, tmp_path, routes, addresses, ok):
    net(routes, addresses)
    runs = tmp_path / "runs"
    runs.mkdir()
    result = check_network(Host(), root=str(runs))
    assert result.ok is ok, result.detail


def test_the_real_rule_fails_without_routes_to_read(net, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    result = check_network(Host(), root=str(runs))
    assert not result.ok and "could not read" in result.detail


# --------------------------------------------------------------------------
# the API process at start


@pytest.fixture
def api(monkeypatch, tmp_path):
    import main
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(trail))
    monkeypatch.delenv("BIOCHEF_EGRESS_MODE", raising=False)
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path))
    return main, trail


def test_a_runner_that_claims_nothing_starts_unchecked(api, monkeypatch):
    main, trail = api
    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    assert main.check_runner_network() is None
    assert not trail.exists()


def test_a_passing_check_is_audited(api, monkeypatch):
    main, trail = api
    monkeypatch.setattr(main, "RUNNER", Scripted("0 1\n"))
    assert main.check_runner_network().ok
    event = json.loads(trail.read_text())
    assert event["event"] == "node.runner.network_check" and event["ok"] is True
    assert event["runner"] == "scripted"


def test_a_failing_check_stops_the_agent_and_is_audited_first(api, monkeypatch):
    main, trail = api
    monkeypatch.setattr(main, "RUNNER", Scripted("1 1\n"))
    with pytest.raises(main.NetworkCheckFailed, match="does not start"):
        main.check_runner_network()
    event = json.loads(trail.read_text())
    assert event["ok"] is False and "1 routes or addresses" in event["detail"]


def test_on_a_node_the_check_reaches_the_node_screen(api, monkeypatch, tmp_path):
    main, trail = api
    from store import db, repo
    from tests.store_support import fresh_database
    bind = fresh_database(tmp_path)[1]
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "manual")
    monkeypatch.setattr(db, "engine", lambda: bind)
    monkeypatch.setattr(main, "RUNNER", Scripted("3 1\n"))
    with pytest.raises(main.NetworkCheckFailed):
        main.check_runner_network()
    with db.reading(bind) as session:
        status = repo.node_status(session)
        assert status.network_check_ok is False
        assert "3 routes or addresses" in status.network_check_detail
        assert status.network_checked_at is not None


def test_the_choice_is_audited_when_the_api_starts(api, monkeypatch):
    from fastapi.testclient import TestClient
    main, trail = api
    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    with TestClient(main.app):
        pass
    events = [json.loads(line)["event"] for line in trail.read_text().splitlines()]
    assert "node.runner.isolation_not_required" in events
