"""Decide whether a bundle was vouched for before anything executes it (#14).

The Hub signs and attests every bundle it publishes and verifies its own work
against a `biochef.signing-policy.v1` document. This reads that same document,
unchanged. A second vocabulary describing the same trust decision would be one
more thing to keep in step, and the failure mode when they drift is that a
bundle the Hub considers signed is one the Agent considers unsigned, or worse,
the reverse.

Three modes, because the honest default and the useful default are different
things here:

    off     do not verify. What the service did before this existed, named so
            that it is a choice rather than an omission.
    warn    verify and report, execute regardless. For an operator finding out
            what their catalogue actually looks like before switching it on.
    strict  verify, and refuse to execute what does not pass. What #14 asks for.

Strict fails CLOSED, in every direction: no cosign on PATH, no policy, an
unreadable policy, a reference that does not belong to the policy's registry, a
manifest whose digest could not be established -- all refusals. A verification
step that passes when it could not run is worse than none, because it is
believed.
"""

import json
import os
import re
import shutil
import subprocess

SCHEMA = "biochef.signing-policy.v1"

OFF = "off"
WARN = "warn"
STRICT = "strict"
MODES = (OFF, WARN, STRICT)

# The same nine identities the Hub's own verifier requires. Read as a set rather
# than picked out individually so a policy carrying unknown keys is refused
# instead of silently half-applied.
REQUIRED = (
    "registry_prefix",
    "certificate_identity",
    "certificate_oidc_issuer",
    "slsa_builder_id",
    "slsa_predicate_type",
    "slsa_build_type",
    "slsa_source_repository",
    "slsa_source_ref",
    "slsa_source_workflow",
)

DEFAULT_TIMEOUT_SECONDS = 60
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class SignatureError(Exception):
    """A bundle did not pass verification, or could not be verified."""


def mode():
    """Which mode this service is running in.

    An unrecognised value is an error rather than a fallback to `off`. A
    typo in BIOCHEF_SIGNING_MODE must not quietly disable the check that the
    operator was trying to turn on -- "strcit" reading as "off" is the exact
    shape of an incident.
    """
    value = os.getenv("BIOCHEF_SIGNING_MODE", OFF).strip().lower()
    if value not in MODES:
        raise SignatureError(
            f"BIOCHEF_SIGNING_MODE is {value!r}, which is not one of "
            f"{', '.join(MODES)}"
        )
    return value


def load_policy(path):
    """The Hub's policy document, validated the way the Hub validates it."""
    if not path:
        raise SignatureError(
            "BIOCHEF_SIGNING_POLICY is not set, and verification cannot be "
            "done without the identities to verify against"
        )
    try:
        with open(path, "r") as handle:
            policy = json.load(handle)
    except OSError as exc:
        raise SignatureError(f"the signing policy could not be read: {exc}") from exc
    except ValueError as exc:
        raise SignatureError(f"the signing policy is not valid JSON: {exc}") from exc

    if not isinstance(policy, dict) or policy.get("schema") != SCHEMA:
        raise SignatureError(f"the signing policy is not {SCHEMA}")
    if set(policy) != {"schema", *REQUIRED}:
        raise SignatureError(
            "the signing policy has missing or unrecognised fields; expected "
            f"exactly {sorted(('schema', *REQUIRED))}"
        )
    if any(not isinstance(policy.get(key), str) or not policy[key]
           for key in REQUIRED):
        raise SignatureError("the signing policy has an empty identity")

    prefix = policy["registry_prefix"]
    if "/" not in prefix or prefix.endswith("/"):
        raise SignatureError(
            f"the signing policy registry prefix {prefix!r} is not a "
            f"registry and repository prefix"
        )
    return policy


def covered_by(reference, policy):
    """Whether this policy has anything to say about this artifact.

    Verifying a signature on an artifact the policy was not written for proves
    nothing about the artifact we are about to run. An operator who points the
    service at a different registry has not thereby made that registry
    trusted -- and without this check, `cosign verify` succeeding against some
    other prefix would look exactly like success against this one.
    """
    return reference.lower().startswith(policy["registry_prefix"].lower())


def is_immutable(reference):
    """Whether a reference already names an exact OCI manifest digest."""
    try:
        repository, digest = reference.rsplit("@", 1)
    except (AttributeError, ValueError):
        return False
    return bool(repository and _DIGEST.fullmatch(digest))


def immutable_reference(reference, digest):
    """Replace a tag, if present, with the manifest digest actually fetched."""
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise SignatureError(
            f"{reference}: the manifest digest could not be established, so there is no immutable artifact reference")
    repository = reference.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    return f"{repository}@{digest}"


def verify(reference, digest, policy, cosign=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Ask cosign whether this exact artifact carries a signature we accept.

    `digest` is required and is the digest of the manifest as served, not a tag.
    A tag is a name the registry can repoint between the moment cosign resolves
    it and the moment we pull, which would verify one artifact and execute
    another -- the same race fetch_tool already avoids by resolving the manifest
    once.
    """
    if not digest:
        raise SignatureError(
            f"{reference}: the manifest digest could not be established, so "
            f"there is nothing to verify a signature against"
        )

    if not covered_by(reference, policy):
        raise SignatureError(
            f"{reference} is not under the policy's registry prefix "
            f"{policy['registry_prefix']!r}; this policy does not describe it"
        )

    binary = cosign or os.getenv("BIOCHEF_COSIGN", "cosign")
    if shutil.which(binary) is None:
        raise SignatureError(
            f"{binary} is not on PATH, so no signature can be checked"
        )

    target = immutable_reference(reference, digest)

    command = [
        binary, "verify",
        "--certificate-identity", policy["certificate_identity"],
        "--certificate-oidc-issuer", policy["certificate_oidc_issuer"],
        target,
    ]
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SignatureError(f"{binary} could not be executed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SignatureError(
            f"{target}: cosign did not answer within {timeout}s"
        ) from exc

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
        raise SignatureError(
            f"{target}: no signature this policy accepts"
            + (f" -- {detail[:600]}" if detail else "")
        )
    return target


def check(reference, digest, log=None):
    """Apply the configured mode to one artifact.

    Returns True when the artifact may be used. Raises in strict mode when it
    may not. The mode is read here rather than passed in so that every call site
    gets the same answer within a process, and so that turning verification on
    does not mean finding every place a bundle is fetched.
    """
    current = mode()
    if current == OFF:
        return True

    try:
        policy = load_policy(os.getenv("BIOCHEF_SIGNING_POLICY"))
        verify(reference, digest, policy)
    except SignatureError as exc:
        if current == STRICT:
            raise
        if log is not None:
            log(f"signature not verified ({exc}); continuing because "
                f"BIOCHEF_SIGNING_MODE is {WARN}")
        return True
    return True
