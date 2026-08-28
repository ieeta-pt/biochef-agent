"""Named starting points for the settings that decide how exposed a run is (#17).

There are sixteen settings, and the ones that matter most are the ones an
operator is least likely to know exist. `BIOCHEF_RUNNER` defaults to running
every tool on the host as this service's user. `BIOCHEF_AUTH` defaults to
answering anybody. Neither default is wrong for a laptop and both are wrong for
a machine holding data, and nothing in a response says which you have.

A profile is a named set of DEFAULTS, not an override. Anything already in the
environment -- including anything from .env -- wins, because a configuration
system that silently discards what an operator explicitly set is worse than one
that makes them set more.

That does mean a profile can be quietly undone by a stray variable, which is why
`describe` exists and why startup prints it. "I selected the tre profile" and
"this service is configured the way tre describes" have to be the same sentence,
and the only way to know is to say what actually took effect.

Enforcement of the egress allowlist is NOT here. E4 says it can be an external
proxy or systemd at this stage, and pretending a Python dictionary constrains
outbound traffic would be the most dangerous kind of documentation. What the tre
profile does is state the allowlist so the thing enforcing it has something to
be checked against.
"""

import os

DEV = "dev"
SERVER = "server"
TRE = "tre"

PROFILES = {
    # A laptop. Nothing here is a boundary, and the profile says so rather than
    # arriving at the same place by leaving everything unset.
    DEV: {
        "BIOCHEF_AUTH": "none",
        "BIOCHEF_RUNNER": "subprocess",
        "BIOCHEF_KEEP_WORKSPACE": "true",
        "REGISTRY_INSECURE": "true",
    },
    # Reachable by other people. Authentication on, tools in containers, and a
    # run's directory removed when it finishes.
    SERVER: {
        "BIOCHEF_AUTH": "bearer",
        "BIOCHEF_RUNNER": "apptainer",
        "BIOCHEF_APPTAINER_ARGS": "--contain",
        "BIOCHEF_KEEP_WORKSPACE": "false",
        "REGISTRY_INSECURE": "false",
    },
    # A trusted research environment. Everything `server` has, and the intent
    # that nothing leaves except to the registry.
    TRE: {
        "BIOCHEF_AUTH": "bearer",
        "BIOCHEF_RUNNER": "apptainer",
        "BIOCHEF_APPTAINER_ARGS": "--contain",
        "BIOCHEF_KEEP_WORKSPACE": "false",
        "REGISTRY_INSECURE": "false",
    },
}

# Stated, not enforced. Whatever does enforce it -- an egress proxy, a systemd
# unit, a network policy -- needs a list to be checked against, and a list that
# lives only in somebody's head is not one. The Aggregator joins this when
# workstream G exists.
TRE_EGRESS_ALLOWLIST = (
    "the OCI registry named by REGISTRY_URL, for pulling tool bundles",
)


class ProfileError(Exception):
    """The named profile does not exist."""


class Applied:
    """What a profile actually did, as opposed to what it describes."""

    def __init__(self, name, applied, overridden):
        self.name = name
        self.applied = applied
        self.overridden = overridden

    def __bool__(self):
        return True


def apply(environ=None):
    """Fill in a profile's defaults, leaving anything already set alone.

    Returns None when no profile was named, which is the historical behaviour
    and stays the default: adding this must not change what an existing
    deployment does.
    """
    environ = os.environ if environ is None else environ

    name = (environ.get("BIOCHEF_PROFILE") or "").strip().lower()
    if not name:
        return None
    if name not in PROFILES:
        raise ProfileError(
            f"BIOCHEF_PROFILE is {name!r}, which is not one of "
            f"{', '.join(sorted(PROFILES))}"
        )

    applied, overridden = [], []
    for key, value in sorted(PROFILES[name].items()):
        existing = environ.get(key)
        if existing is not None and existing != "":
            if existing != value:
                overridden.append((key, value, existing))
            continue
        environ[key] = value
        applied.append((key, value))
    return Applied(name, applied, overridden)


def describe(result):
    """What to print at startup.

    Every line an operator needs to tell "I asked for tre" apart from "this is
    what tre asks for". A profile that is mostly overridden is a normal thing to
    do and a dangerous thing to do by accident, so the overrides are listed
    rather than counted.
    """
    if result is None:
        return "No profile selected; every setting is its own default."

    lines = [f"Profile {result.name!r}:"]
    for key, value in result.applied:
        lines.append(f"  {key}={value}")
    for key, value, existing in result.overridden:
        lines.append(
            f"  {key}={existing}   (profile asks for {value}; "
            f"the environment already set this and wins)"
        )
    if result.name == TRE:
        lines.append("  egress is expected to be restricted to:")
        for entry in TRE_EGRESS_ALLOWLIST:
            lines.append(f"    - {entry}")
        lines.append("  which this service states and does NOT enforce.")
    return "\n".join(lines)
