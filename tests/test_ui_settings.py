"""The UI refuses to start on a half configured setting (section 4)."""

import os
import re
import subprocess
import sys

import pytest

import ui.__main__ as entry
from node import keys as node_keys
from store import migrate
from tests.ui_support import REPO_ROOT, USER_HEADER
from ui import app as ui_app
from ui import settings
from ui.app import create_app
from ui.settings import SettingsError

NAMES = ("BIOCHEF_UI_IDENTITY", "BIOCHEF_UI_IDENTITY_HEADER",
         "BIOCHEF_UI_TRUSTED_PROXIES", "BIOCHEF_UI_DEV_USER", "BIOCHEF_UI_HOST",
         "BIOCHEF_UI_PORT", "BIOCHEF_POLICY_ENGINE_URL")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("BIOCHEF_NODE_NAME", "Example TRE (test)")
    return monkeypatch


@pytest.fixture
def dev(env):
    env.setenv("BIOCHEF_UI_IDENTITY", "dev")
    env.setenv("BIOCHEF_UI_DEV_USER", "dev.user")
    return env


@pytest.fixture
def header(env):
    env.setenv("BIOCHEF_UI_IDENTITY", "header")
    env.setenv("BIOCHEF_UI_IDENTITY_HEADER", USER_HEADER)
    env.setenv("BIOCHEF_UI_TRUSTED_PROXIES", "10.0.0.0/24, fd00::/8")
    return env


def test_header_mode_loads(header):
    loaded = settings.load()
    assert (loaded.identity, loaded.identity_header) == ("header", USER_HEADER)
    assert [str(network) for network in loaded.trusted_proxies] == [
        "10.0.0.0/24", "fd00::/8"]


def test_dev_mode_loads_with_the_defaults_of_the_spec(dev):
    loaded = settings.load()
    assert (loaded.host, loaded.port, loaded.dev_user) == (
        "127.0.0.1", 8081, "dev.user")


@pytest.mark.parametrize("value", [None, "", "none", "HEADER", "bearer"])
def test_there_is_no_default_identity(env, value):
    if value is not None:
        env.setenv("BIOCHEF_UI_IDENTITY", value)
    with pytest.raises(SettingsError, match="BIOCHEF_UI_IDENTITY"):
        settings.load()


@pytest.mark.parametrize("mode", ["dev", "header"])
def test_no_audit_trail_no_page(dev, mode):
    dev.setenv("BIOCHEF_UI_IDENTITY", mode)
    dev.delenv("BIOCHEF_AUDIT_LOG")
    with pytest.raises(SettingsError, match="BIOCHEF_AUDIT_LOG"):
        settings.load()


def test_a_node_has_a_name(dev):
    dev.delenv("BIOCHEF_NODE_NAME")
    with pytest.raises(SettingsError, match="BIOCHEF_NODE_NAME"):
        settings.load()


@pytest.mark.parametrize("name", ["BIOCHEF_UI_IDENTITY_HEADER",
                                  "BIOCHEF_UI_TRUSTED_PROXIES"])
def test_header_mode_needs_both_of_its_settings(header, name):
    header.delenv(name)
    with pytest.raises(SettingsError, match=name):
        settings.load()


@pytest.mark.parametrize("name", ["X Forwarded", "X:User", "é"])
def test_a_header_name_has_to_be_one(header, name):
    header.setenv("BIOCHEF_UI_IDENTITY_HEADER", name)
    with pytest.raises(SettingsError, match="not a header name"):
        settings.load()


@pytest.mark.parametrize("value", ["proxy.tre", "10.0.0.5/24", "10.0.0.0/33",
                                   "10.0.0.0/24,", "10.0.0.0/24,nonsense"])
def test_a_proxy_list_is_cidrs(header, value):
    header.setenv("BIOCHEF_UI_TRUSTED_PROXIES", value)
    with pytest.raises(SettingsError, match="not a CIDR"):
        settings.load()


@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "10.0.0.0/8,0.0.0.0/0"])
def test_trusting_every_address_is_refused(header, value):
    header.setenv("BIOCHEF_UI_TRUSTED_PROXIES", value)
    with pytest.raises(SettingsError, match="every address"):
        settings.load()


def test_dev_mode_needs_a_user(dev):
    dev.delenv("BIOCHEF_UI_DEV_USER")
    with pytest.raises(SettingsError, match="BIOCHEF_UI_DEV_USER"):
        settings.load()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "localhost"])
def test_dev_mode_stays_on_loopback(dev, host):
    dev.setenv("BIOCHEF_UI_HOST", host)
    with pytest.raises(SettingsError, match="127.0.0.1 or ::1"):
        settings.load()


def test_dev_mode_accepts_the_ipv6_loopback(dev):
    dev.setenv("BIOCHEF_UI_HOST", "::1")
    assert settings.load().host == "::1"


def test_header_mode_can_bind_where_the_tre_needs(header):
    header.setenv("BIOCHEF_UI_HOST", "10.0.0.9")
    assert settings.load().host == "10.0.0.9"


@pytest.mark.parametrize("port", ["0", "65536", "http", "-1"])
def test_a_port_is_a_port(dev, port):
    dev.setenv("BIOCHEF_UI_PORT", port)
    with pytest.raises(SettingsError, match="BIOCHEF_UI_PORT"):
        settings.load()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x",
                                 "policy.tre", "https://"])
def test_the_policy_engine_link_cannot_run_anything(dev, url):
    dev.setenv("BIOCHEF_POLICY_ENGINE_URL", url)
    with pytest.raises(SettingsError, match="BIOCHEF_POLICY_ENGINE_URL"):
        settings.load()


def test_the_policy_engine_is_optional(dev):
    assert settings.load().policy_engine_url is None
    dev.setenv("BIOCHEF_POLICY_ENGINE_URL", "https://policy.tre.test/")
    assert settings.load().policy_engine_url == "https://policy.tre.test/"


def test_cookies_are_secure_behind_the_proxy_only(dev):
    assert settings.load().secure_cookies is False
    dev.setenv("BIOCHEF_UI_IDENTITY", "header")
    dev.setenv("BIOCHEF_UI_IDENTITY_HEADER", USER_HEADER)
    dev.setenv("BIOCHEF_UI_TRUSTED_PROXIES", "10.0.0.0/24")
    assert settings.load().secure_cookies is True


def test_the_visa_comes_from_the_passport_setting(dev):
    assert settings.load().required_visa is None
    dev.setenv("BIOCHEF_PASSPORT_REQUIRE_VISA", "ControlledAccessGrants")
    assert settings.load().required_visa == "ControlledAccessGrants"


# What a decision needs, checked at start


@pytest.fixture
def ready(dev, tmp_path):
    node_keys.init(tmp_path / "keys")
    dev.setenv("BIOCHEF_NODE_SIGNING_KEY", str(tmp_path / "keys" / node_keys.SIGNING_FILE))
    dev.setenv("BIOCHEF_NODE_ENCRYPTION_KEY",
               str(tmp_path / "keys" / node_keys.ENCRYPTION_FILE))
    url = f"sqlite:///{tmp_path}/agent.db"
    dev.setenv("BIOCHEF_DB_URL", url)
    return url


def test_a_configured_page_starts(ready):
    migrate.upgrade(ready)
    assert ui_app.configured_app().state.keys is not None


def test_no_page_on_a_database_that_is_not_migrated(ready):
    with pytest.raises(SettingsError, match="python -m store migrate"):
        ui_app.configured_app()


def test_no_page_without_the_signing_key(ready, dev):
    migrate.upgrade(ready)
    dev.delenv("BIOCHEF_NODE_SIGNING_KEY")
    with pytest.raises(node_keys.NodeKeyError, match="BIOCHEF_NODE_SIGNING_KEY"):
        ui_app.configured_app()


def test_the_fake_policy_engine_is_for_development_only(ready, dev):
    migrate.upgrade(ready)
    assert ui_app.configured_app().state.policy.name == "Fake Policy Engine"
    dev.setenv("BIOCHEF_UI_IDENTITY", "header")
    dev.setenv("BIOCHEF_UI_IDENTITY_HEADER", USER_HEADER)
    dev.setenv("BIOCHEF_UI_TRUSTED_PROXIES", "10.0.0.0/24")
    assert ui_app.configured_app().state.policy is None


# The processes, as started


def run_python(*args):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)


def test_the_asgi_entry_refuses_to_start_unconfigured():
    result = run_python("-c", "import ui.app; ui.app.app")
    assert result.returncode != 0
    assert "SettingsError" in result.stderr
    assert "BIOCHEF_UI_IDENTITY" in result.stderr


def test_the_command_refuses_with_one_line():
    result = run_python("-m", "ui")
    assert result.returncode == 1
    assert result.stderr.startswith("BIOCHEF_UI_IDENTITY must be")
    assert "Traceback" not in result.stderr


def test_the_page_does_not_import_the_api():
    code = ("import sys, ui.app; "
            "print(sorted({'main', 'runs'} & set(sys.modules)))")
    result = run_python("-c", code)
    assert result.stdout.strip() == "[]", result.stderr


def test_the_command_keeps_proxy_headers_off(dev, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(entry.uvicorn, "run",
                        lambda app, **options: seen.update(options))
    monkeypatch.setattr(entry.app, "configured_app", lambda: create_app(
        settings.load(), bind=None, keys=None))
    assert entry.main() == 0
    assert seen == {"host": "127.0.0.1", "port": 8081, "proxy_headers": False}


@pytest.mark.parametrize("version,accepted", [
    ((3, 10), False), ((3, 11), True), ((3, 13), True), ((3, 14), False)])
def test_run_ui_refuses_a_python_ci_does_not_test(version, accepted):
    script = (REPO_ROOT / "run-ui.sh").read_text()
    guard = re.search(r"python -c '([^']+)' \|\|", script).group(1)
    claim = f"import sys; sys.version_info = {version + (0,)}; "
    assert (run_python("-c", claim + guard).returncode == 0) is accepted


def test_run_ui_uses_that_command():
    script = (REPO_ROOT / "run-ui.sh").read_text()
    assert "exec python -m ui" in script
    assert "uvicorn" not in script.replace("uvicorn's", "")
    assert os.access(REPO_ROOT / "run-ui.sh", os.X_OK)


# Dataset bindings


def test_bindings_are_off_by_default(dev):
    loaded = settings.load()
    assert (loaded.bindings_from_ui, loaded.bindings_file) == (False, None)


@pytest.mark.parametrize("value", ["yes", "TRUE", "1"])
def test_the_switch_is_true_or_false(dev, value):
    dev.setenv("BIOCHEF_DATASET_BINDINGS_FROM_UI", value)
    with pytest.raises(SettingsError, match="true or false"):
        settings.load()


@pytest.mark.parametrize("name,value", [
    ("BIOCHEF_DATASET_BINDINGS_FROM_UI", "true"),
    ("BIOCHEF_DATASET_BINDINGS_FILE", "/etc/biochef/datasets.yaml")])
def test_bindings_need_localpath_and_a_root(dev, tmp_path, name, value):
    dev.setenv(name, value)
    dev.setenv("BIOCHEF_DATA_SOURCES", "upload")
    dev.setenv("BIOCHEF_LOCAL_ROOT", str(tmp_path))
    with pytest.raises(SettingsError, match="localpath"):
        settings.load()
    dev.setenv("BIOCHEF_DATA_SOURCES", "upload,localpath")
    dev.setenv("BIOCHEF_LOCAL_ROOT", str(tmp_path / "missing"))
    with pytest.raises(SettingsError, match="BIOCHEF_LOCAL_ROOT"):
        settings.load()
    dev.setenv("BIOCHEF_LOCAL_ROOT", str(tmp_path))
    assert settings.load().local_root == str(tmp_path)


def test_a_bad_bindings_file_stops_the_page(ready, dev, tmp_path):
    migrate.upgrade(ready)
    (tmp_path / "data").mkdir()
    bad = tmp_path / "datasets.yaml"
    bad.write_text("EGAD1:\n  type: localpath\n  location: ../..\n")
    dev.setenv("BIOCHEF_DATA_SOURCES", "localpath")
    dev.setenv("BIOCHEF_LOCAL_ROOT", str(tmp_path / "data"))
    dev.setenv("BIOCHEF_DATASET_BINDINGS_FILE", str(bad))
    with pytest.raises(SettingsError, match="outside the data root"):
        ui_app.configured_app()


def test_a_good_bindings_file_is_synced_at_start(ready, dev, tmp_path):
    migrate.upgrade(ready)
    (tmp_path / "data" / "cohort").mkdir(parents=True)
    good = tmp_path / "datasets.yaml"
    good.write_text("EGAD1:\n  type: localpath\n  location: cohort\n")
    dev.setenv("BIOCHEF_DATA_SOURCES", "localpath")
    dev.setenv("BIOCHEF_LOCAL_ROOT", str(tmp_path / "data"))
    dev.setenv("BIOCHEF_DATASET_BINDINGS_FILE", str(good))
    app = ui_app.configured_app()
    from store import db, repo
    with db.reading(app.state.bind) as session:
        assert repo.dataset_binding(session, "EGAD1").source == "config"
