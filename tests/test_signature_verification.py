"""What proves a bundle came from the Hub before we execute it (#14, E1b).

The Hub half of E1 was already merged when this was written: publish-recipes.yml
installs cosign, runs `cosign sign` and `cosign attest` over every published
bundle, and verifies its own work against a `biochef.signing-policy.v1`
document. The browser half landed in Biochef#96. The Agent verified nothing.

Digest validation, which this branch is built on, answers a different question,
and having the first is easy to mistake for having the second:

    digest    "are these the bytes the manifest named?"
    signature "did anyone we trust ever vouch for this manifest?"

A registry serving a manifest of its own making passes the digest check
perfectly, because the blobs match the digests in the manifest it also made up.

What follows pins the behaviour that closes that. The emphasis is on the ways
verification can be present and still worthless: a mode that silently reads as
`off`, a policy describing some other registry, a missing cosign, a digest that
was never established. Each of those is a pass that would be believed.
"""

import hashlib
import json
import os
import stat
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

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import pytest

import convert
import signing


POLICY = {
    "schema": "biochef.signing-policy.v1",
    "registry_prefix": "registry.example.test/biochef-plugins-",
    "certificate_identity": "https://github.com/ieeta-pt/biochef-hub/.github/workflows/publish-recipes.yml@refs/heads/master",
    "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
    "slsa_builder_id": "https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_generic_slsa3.yml@refs/tags/v2.1.0",
    "slsa_predicate_type": "https://slsa.dev/provenance/v0.2",
    "slsa_build_type": "https://github.com/slsa-framework/slsa-github-generator/generic@v1",
    "slsa_source_repository": "github.com/ieeta-pt/biochef-recipes",
    "slsa_source_ref": "refs/heads/master",
    "slsa_source_workflow": "ieeta-pt/biochef-recipes/.github/workflows/manual-publish.yml@refs/heads/master",
}

REFERENCE = "registry.example.test/biochef-plugins-samtools.view:1.0"
DIGEST = "sha256:" + "ab" * 32


def _policy_file(tmp_path, **overrides):
    document = dict(POLICY)
    document.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    return str(path)


def _stub_cosign(tmp_path, exit_code, message="stub cosign"):
    """A cosign that answers however the test needs, on PATH.

    A stub rather than the real thing because the real one needs a registry, a
    Fulcio certificate and a Rekor entry. What is being tested here is what this
    service does with cosign's answer -- which is where the decisions are.
    """
    path = tmp_path / "cosign"
    path.write_text(
        "#!/bin/sh\n"
        f"echo '{message}' >&2\n"
        f"exit {exit_code}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# --- the mode switch ---------------------------------------------------------

def test_off_is_the_default(monkeypatch):
    monkeypatch.delenv("BIOCHEF_SIGNING_MODE", raising=False)
    assert signing.mode() == signing.OFF


def test_a_mistyped_mode_is_an_error_and_not_silently_off(monkeypatch):
    """The failure this exists to prevent.

    An operator turning verification on and typing `strcit` must not get a
    service that verifies nothing and says nothing. Falling back to `off` here
    would be indistinguishable from the setting having worked.
    """
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strcit")
    with pytest.raises(signing.SignatureError) as caught:
        signing.mode()
    assert "strcit" in str(caught.value)


def test_off_does_not_verify_and_does_not_need_a_policy(monkeypatch):
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "off")
    monkeypatch.delenv("BIOCHEF_SIGNING_POLICY", raising=False)
    assert signing.check(REFERENCE, DIGEST) is True


# --- the policy document -----------------------------------------------------

def test_the_hub_policy_schema_is_what_is_read(tmp_path):
    """The Hub's own document, not a second vocabulary for the same decision."""
    policy = signing.load_policy(_policy_file(tmp_path))
    assert policy["certificate_identity"] == POLICY["certificate_identity"]
    assert set(policy) == {"schema", *signing.REQUIRED}


def test_a_policy_of_another_schema_is_refused(tmp_path):
    with pytest.raises(signing.SignatureError):
        signing.load_policy(_policy_file(tmp_path, schema="something.else.v1"))


def test_a_policy_with_an_unknown_field_is_refused(tmp_path):
    document = dict(POLICY)
    document["extra"] = "x"
    path = tmp_path / "p.json"
    path.write_text(json.dumps(document))
    with pytest.raises(signing.SignatureError):
        signing.load_policy(str(path))


def test_a_policy_with_an_empty_identity_is_refused(tmp_path):
    with pytest.raises(signing.SignatureError):
        signing.load_policy(_policy_file(tmp_path, certificate_identity=""))


def test_a_missing_policy_path_is_refused(tmp_path):
    with pytest.raises(signing.SignatureError):
        signing.load_policy(None)
    with pytest.raises(signing.SignatureError):
        signing.load_policy(str(tmp_path / "absent.json"))


# --- what the policy actually covers ----------------------------------------

def test_a_reference_outside_the_policys_registry_is_refused(tmp_path):
    """Verifying the wrong artifact is not verifying.

    An operator who repoints REGISTRY_URL has not made that registry trusted.
    Without this, cosign succeeding against some other prefix would look exactly
    like success against the one the policy describes.
    """
    policy = signing.load_policy(_policy_file(tmp_path))
    assert not signing.covered_by("other.registry/whatever:1", policy)
    with pytest.raises(signing.SignatureError) as caught:
        signing.verify("other.registry/whatever:1", DIGEST, policy,
                       cosign=_stub_cosign(tmp_path, 0))
    assert "registry prefix" in str(caught.value)


# --- fail closed -------------------------------------------------------------

def test_a_missing_digest_is_refused(tmp_path):
    """A tag is not an artifact.

    cosign resolving a tag itself would verify whatever the tag points at when
    it looks, which is not necessarily what was pulled -- the race fetch_tool
    already avoids by resolving the manifest once.
    """
    policy = signing.load_policy(_policy_file(tmp_path))
    with pytest.raises(signing.SignatureError) as caught:
        signing.verify(REFERENCE, None, policy, cosign=_stub_cosign(tmp_path, 0))
    assert "digest" in str(caught.value)


def test_a_missing_cosign_is_refused_rather_than_skipped(tmp_path):
    policy = signing.load_policy(_policy_file(tmp_path))
    with pytest.raises(signing.SignatureError) as caught:
        signing.verify(REFERENCE, DIGEST, policy,
                       cosign=str(tmp_path / "no-such-cosign"))
    assert "PATH" in str(caught.value)


def test_strict_refuses_when_cosign_rejects(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv("BIOCHEF_COSIGN", _stub_cosign(tmp_path, 1, "no matching signatures"))
    with pytest.raises(signing.SignatureError) as caught:
        signing.check(REFERENCE, DIGEST)
    assert "no matching signatures" in str(caught.value)


def test_strict_accepts_when_cosign_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv("BIOCHEF_COSIGN", _stub_cosign(tmp_path, 0))
    assert signing.check(REFERENCE, DIGEST) is True


def test_strict_without_a_policy_refuses(tmp_path, monkeypatch):
    """Turning verification on without saying what to verify against.

    Refusing is the only safe reading: the alternative is a service that
    believes it is in strict mode and checks nothing.
    """
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.delenv("BIOCHEF_SIGNING_POLICY", raising=False)
    with pytest.raises(signing.SignatureError):
        signing.check(REFERENCE, DIGEST)


def test_warn_reports_and_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "warn")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv("BIOCHEF_COSIGN", _stub_cosign(tmp_path, 1, "no matching signatures"))
    said = []
    assert signing.check(REFERENCE, DIGEST, log=said.append) is True
    assert said and "not verified" in said[0]


def test_cosign_is_asked_about_the_digest_and_not_the_tag(tmp_path, monkeypatch):
    """What is verified must be what was resolved.

    The stub records its arguments so this asserts on the reference actually
    passed, rather than on the fact that verification returned.
    """
    recorder = tmp_path / "argv"
    cosign = tmp_path / "cosign"
    cosign.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + str(recorder) + "\nexit 0\n")
    cosign.chmod(cosign.stat().st_mode | stat.S_IEXEC)
    policy = signing.load_policy(_policy_file(tmp_path))
    signing.verify(REFERENCE, DIGEST, policy, cosign=str(cosign))
    argv = recorder.read_text().split("\n")
    assert f"registry.example.test/biochef-plugins-samtools.view@{DIGEST}" in argv
    assert POLICY["certificate_identity"] in argv
    assert POLICY["certificate_oidc_issuer"] in argv
    assert not any(a.endswith(":1.0") for a in argv), "the tag was verified, not the digest"


# --- the digest the signature is checked against ----------------------------

class _Response:
    def __init__(self, body, headers=None, status=200):
        self.content = body
        self.headers = headers or {}
        self.status_code = status


class _RawRegistry:
    """A client that supports a raw request, as the real oras Registry does."""

    prefix = "https"

    def __init__(self, body, headers=None, status=200):
        self._response = _Response(body, headers, status)

    def get_container(self, target):
        class _C:
            def manifest_url(self):
                return "registry.example.test/v2/x/manifests/1.0"
        return _C()

    def do_request(self, url, method="GET", headers=None):
        return self._response


def test_the_digest_comes_from_the_bytes_that_were_served(monkeypatch):
    body = b'{"layers":[],"schemaVersion":2}'
    monkeypatch.setattr(convert, "client", _RawRegistry(body))
    manifest, digest = convert.fetch_manifest("registry.example.test/x:1.0")
    assert manifest["schemaVersion"] == 2
    assert digest == "sha256:" + hashlib.sha256(body).hexdigest()


def test_a_registry_that_mislabels_its_own_manifest_is_refused(monkeypatch):
    """The header is advisory and comes from the same party as the manifest.

    If the two disagree there is no winner to pick: the registry is answering
    one thing and calling it another.
    """
    body = b'{"layers":[],"schemaVersion":2}'
    wrong = {"Docker-Content-Digest": "sha256:" + "00" * 32}
    monkeypatch.setattr(convert, "client", _RawRegistry(body, headers=wrong))
    with pytest.raises(convert.ToolIntegrityError) as caught:
        convert.fetch_manifest("registry.example.test/x:1.0")
    assert "labelled" in str(caught.value)


def test_a_non_200_manifest_response_is_refused(monkeypatch):
    monkeypatch.setattr(convert, "client", _RawRegistry(b"nope", status=404))
    with pytest.raises(convert.ToolIntegrityError):
        convert.fetch_manifest("registry.example.test/x:1.0")


def test_a_client_without_a_raw_request_yields_no_digest_and_strict_refuses(monkeypatch, tmp_path):
    """The compatibility path cannot become a way to skip verification.

    Older clients, and the stubs the rest of this suite installs, cannot produce
    a manifest digest. That must degrade to a refusal under strict, not to a
    pass.
    """
    class _Plain:
        def get_container(self, target):
            return object()

        def get_manifest(self, container):
            return {"layers": []}

    monkeypatch.setattr(convert, "client", _Plain())
    manifest, digest = convert.fetch_manifest("registry.example.test/x:1.0")
    assert manifest == {"layers": []}
    assert digest is None

    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv("BIOCHEF_COSIGN", _stub_cosign(tmp_path, 0))
    with pytest.raises(signing.SignatureError):
        signing.check(REFERENCE, digest)


# --- the wiring --------------------------------------------------------------

def test_the_check_happens_before_the_manifest_is_used_for_anything(monkeypatch):
    """Order matters, and it is not obvious from the outside.

    cache_matches decides whether the cached bundle is still good by comparing
    against this manifest, and verify_against_manifest checks pulled blobs
    against the digests it declares. Both trust it. A manifest nobody vouched
    for makes both self-consistent and meaningless, so the signature check has
    to come first.
    """
    body = Path(convert.__file__).read_text()
    fetch = body.split("def fetch_tool")[1].split("\ndef ")[0]

    # Comments stripped first. The comment explaining WHY the check precedes the
    # cache decision names cache_matches, so searching the raw text finds the
    # prose before the call and reports the order backwards -- which is exactly
    # what this assertion did when it was first written.
    code = "\n".join(line.split("#", 1)[0] for line in fetch.splitlines())

    assert "signing.check(" in code, "fetch_tool no longer verifies anything"
    assert "cache_matches(" in code, "read the wrong function"
    assert code.index("signing.check(") < code.index("cache_matches("), (
        "the signature is checked after the cache decision, which already "
        "trusted the manifest"
    )
