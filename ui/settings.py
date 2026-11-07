"""The UI's settings, or a refusal to start.

The same rule as auth.py and audit.py: a half configured setting stops the
process, because each way of guessing here fails open. A page that starts
without a trail makes decisions nobody can account for, and one that starts
without knowing who may vouch for a user trusts everybody.

This process does not import convert.py, which is what loads .env for the API,
so it loads it itself.
"""

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv

import audit
import runner

load_dotenv()

HEADER = "header"
DEV = "dev"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8081"
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

# RFC 9110 field-name. Anything else is not a header a proxy could set.
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class SettingsError(Exception):
    """The UI cannot start like this."""


@dataclass(frozen=True)
class Settings:
    identity: str
    node_name: str
    host: str = DEFAULT_HOST
    port: int = int(DEFAULT_PORT)
    identity_header: str | None = None
    trusted_proxies: tuple = ()
    dev_user: str | None = None
    policy_engine_url: str | None = None
    required_visa: str | None = None
    local_root: str | None = None
    bindings_file: str | None = None
    bindings_from_ui: bool = False
    egress_mode: str = "airlock"
    runner: str = "subprocess"
    signing_mode: str = "off"
    max_concurrent_runs: int = 4
    max_retained_runs: int = 32
    keep_outputs_seconds: int = 3600
    poll_seconds: int = 30
    network_isolated: bool = False

    @property
    def secure_cookies(self):
        # Dev is plain http on a loopback address, where a Secure cookie would
        # never come back. In header mode the browser only ever talks to the proxy.
        return self.identity == HEADER


def _setting(value):
    return (value or "").strip() or None


def _required(value, name, why):
    if value is None:
        raise SettingsError(f"{name} is required: {why}")
    return value


def _port(value):
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise SettingsError(f"BIOCHEF_UI_PORT must be a port number, not {value!r}")
    return port


def _proxies(value):
    networks = []
    for item in value.split(","):
        try:
            network = ipaddress.ip_network(item.strip())
        except ValueError:
            raise SettingsError(f"BIOCHEF_UI_TRUSTED_PROXIES: {item.strip()!r} "
                                f"is not a CIDR with no host bits") from None
        if network.prefixlen == 0:
            # Trusting every address is the same as checking none of them.
            raise SettingsError(f"BIOCHEF_UI_TRUSTED_PROXIES: {network} trusts "
                                f"every address")
        networks.append(network)
    return tuple(networks)


def _policy_engine(value):
    if value is None:
        return None
    # It ends up in an href, where `javascript:` would run in the page.
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingsError("BIOCHEF_POLICY_ENGINE_URL must be an http or https URL")
    return value


def _flag(value, name):
    if value in (None, "false"):
        return False
    if value == "true":
        return True
    raise SettingsError(f"{name} must be true or false, not {value!r}")


def _bindings():
    """The dataset settings. Bindings need the source that can read them."""
    from_ui = _flag(_setting(os.getenv("BIOCHEF_DATASET_BINDINGS_FROM_UI")),
                    "BIOCHEF_DATASET_BINDINGS_FROM_UI")
    bindings_file = _setting(os.getenv("BIOCHEF_DATASET_BINDINGS_FILE"))
    root = _setting(os.getenv("BIOCHEF_LOCAL_ROOT"))
    if from_ui or bindings_file:
        sources = os.getenv("BIOCHEF_DATA_SOURCES") or ""
        if "localpath" not in [item.strip() for item in sources.split(",")]:
            raise SettingsError("dataset bindings need localpath in "
                                "BIOCHEF_DATA_SOURCES")
        if root is None or not os.path.isdir(root):
            raise SettingsError("dataset bindings need BIOCHEF_LOCAL_ROOT set to "
                                "an existing directory")
    return {"local_root": root, "bindings_file": bindings_file,
            "bindings_from_ui": from_ui}


def _whole(name, value):
    try:
        return int(value)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, not {value!r}") from None


def _node_facts():
    """What the Node screen reports. The processes that use them still decide them."""
    return {
        "runner": _setting(os.getenv("BIOCHEF_RUNNER")) or "subprocess",
        "signing_mode": (_setting(os.getenv("BIOCHEF_SIGNING_MODE")) or "off").lower(),
        "max_concurrent_runs": _whole(
            "BIOCHEF_MAX_CONCURRENT_RUNS",
            _setting(os.getenv("BIOCHEF_MAX_CONCURRENT_RUNS")) or "4"),
        "max_retained_runs": _whole(
            "BIOCHEF_MAX_RETAINED_RUNS",
            _setting(os.getenv("BIOCHEF_MAX_RETAINED_RUNS")) or "32"),
        "keep_outputs_seconds": _whole(
            "BIOCHEF_KEEP_OUTPUTS", _setting(os.getenv("BIOCHEF_KEEP_OUTPUTS")) or "3600"),
        "poll_seconds": _whole(
            "BIOCHEF_COORDINATOR_POLL_SECONDS",
            _setting(os.getenv("BIOCHEF_COORDINATOR_POLL_SECONDS")) or "30"),
    }


def _isolation(name):
    try:
        return runner.get_runner(name).isolates_network
    except ValueError as refused:
        raise SettingsError(str(refused)) from None


def _egress_mode():
    value = _setting(os.getenv("BIOCHEF_EGRESS_MODE"))
    if value not in ("airlock", "manual"):
        raise SettingsError("BIOCHEF_EGRESS_MODE must be `airlock` or `manual`; there "
                            "is no default, because each one lets results leave "
                            "differently")
    return value


def load():
    """The settings of this process, read from the environment."""
    mode = _setting(os.getenv("BIOCHEF_UI_IDENTITY"))
    if mode not in (HEADER, DEV):
        raise SettingsError("BIOCHEF_UI_IDENTITY must be `header` or `dev`; there "
                            "is no default, because each one trusts someone "
                            "different")
    if audit.path() is None:
        raise SettingsError("BIOCHEF_AUDIT_LOG is required: every decision made "
                            "on this page has to reach the audit trail")

    host = _setting(os.getenv("BIOCHEF_UI_HOST")) or DEFAULT_HOST
    fields = {
        "identity": mode,
        "host": host,
        "port": _port(_setting(os.getenv("BIOCHEF_UI_PORT")) or DEFAULT_PORT),
        "node_name": _required(_setting(os.getenv("BIOCHEF_NODE_NAME")),
                               "BIOCHEF_NODE_NAME", "it names this node"),
        "policy_engine_url": _policy_engine(
            _setting(os.getenv("BIOCHEF_POLICY_ENGINE_URL"))),
        "required_visa": _setting(os.getenv("BIOCHEF_PASSPORT_REQUIRE_VISA")),
        "egress_mode": _egress_mode(),
        **_node_facts(),
        "network_isolated": _isolation(
            _setting(os.getenv("BIOCHEF_RUNNER")) or "subprocess"),
        **_bindings(),
    }

    if mode == HEADER:
        name = _required(_setting(os.getenv("BIOCHEF_UI_IDENTITY_HEADER")),
                         "BIOCHEF_UI_IDENTITY_HEADER",
                         "it carries the user name in header mode")
        if not HEADER_NAME.fullmatch(name):
            raise SettingsError(f"BIOCHEF_UI_IDENTITY_HEADER: {name!r} is not a "
                                f"header name")
        proxies = _required(_setting(os.getenv("BIOCHEF_UI_TRUSTED_PROXIES")),
                            "BIOCHEF_UI_TRUSTED_PROXIES",
                            "it says who may set that header")
        fields |= {"identity_header": name, "trusted_proxies": _proxies(proxies)}
    else:
        if host not in LOOPBACK_HOSTS:
            raise SettingsError(f"dev identity gives every visitor the same name, "
                                f"so BIOCHEF_UI_HOST must be 127.0.0.1 or ::1, "
                                f"not {host}")
        fields["dev_user"] = _required(_setting(os.getenv("BIOCHEF_UI_DEV_USER")),
                                       "BIOCHEF_UI_DEV_USER",
                                       "dev identity needs a name to record")
    return Settings(**fields)
