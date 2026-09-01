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

import asyncio
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
import evidence_verification
import main
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
    "slsa_source_workflow": "ieeta-pt/biochef-recipes/.github/workflows/upload.yml@refs/heads/master",
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
            digest = target.rsplit("@", 1)[1] if "@" in target else None

            def manifest_url(self):
                return "registry.example.test/v2/x/manifests/1.0"
        return _C()

    def do_request(self, url, method="GET", headers=None):
        return self._response


def test_the_digest_comes_from_the_bytes_that_were_served(monkeypatch):
    body = b'{"layers":[],"schemaVersion":2}'
    monkeypatch.setattr(convert, "client", _RawRegistry(body))
    manifest, digest, raw_manifest = convert.fetch_manifest("registry.example.test/x:1.0")
    assert manifest["schemaVersion"] == 2
    assert digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert raw_manifest == body


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


def test_a_digest_reference_must_return_the_manifest_it_names(monkeypatch):
    body = b'{"layers":[],"schemaVersion":2}'
    registry = _RawRegistry(body)
    expected = "sha256:" + "00" * 32
    container = registry.get_container("unused")
    container.digest = expected
    registry.get_container = lambda target: container
    monkeypatch.setattr(convert, "client", registry)
    with pytest.raises(convert.ToolIntegrityError) as caught:
        convert.fetch_manifest(f"registry.example.test/x@{expected}")
    assert "immutable reference" in str(caught.value)


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
    manifest, digest, raw_manifest = convert.fetch_manifest("registry.example.test/x:1.0")
    assert manifest == {"layers": []}
    assert digest is None
    assert raw_manifest is None

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
    assert "evidence_verification.check(" in code, "direct evidence is no longer verified"
    assert "evidence_verification.verify_pulled(" in code, "pulled evidence is no longer bound"
    assert "cache_matches(" in code, "read the wrong function"
    assert code.index("signing.check(") < code.index("cache_matches("), (
        "the signature is checked after the cache decision, which already "
        "trusted the manifest"
    )
    assert code.index("evidence_verification.check(") < code.index("cache_matches(")
    assert code.index("cache_matches(") < code.index("evidence_verification.verify_pulled(")


def test_strict_requires_the_caller_to_select_an_immutable_digest(monkeypatch):
    body = b'{"layers":[],"schemaVersion":2}'
    registry = _RefusingRegistry(body)

    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", "registry.example.test")
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    with pytest.raises(signing.SignatureError, match="caller.*immutable"):
        convert.fetch_tool("jq.query-1", "biochef-plugins-jq.query:latest")
    assert not registry.pulled


def test_verification_refusals_have_a_structured_http_response():
    handler = main.app.exception_handlers[signing.SignatureError]
    assert handler is main.app.exception_handlers[
        evidence_verification.EvidenceVerificationError
    ]

    response = asyncio.run(handler(None, signing.SignatureError("not admitted")))
    assert response.status_code == 403
    assert json.loads(response.body) == {
        "detail": {
            "error": "artifact_verification",
            "message": "not admitted",
        }
    }


def test_every_fetch_repeats_all_checks_even_when_the_bundle_is_cached(
        tmp_path, monkeypatch):
    """A warm cache must not reduce execution verification to pull-time history."""
    body = b'{"layers":[],"schemaVersion":2}'
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    subject = f"registry.example.test/biochef-plugins-jq.query@{digest}"
    registry = _RefusingRegistry(body)
    cache = tmp_path / "cache" / "jq.query"
    cache.mkdir(parents=True)
    (cache / "bundle.json").write_text('{"bin":"jq"}')
    events = []
    direct_result = object()

    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", "registry.example.test")
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setattr(
        convert.signing,
        "check",
        lambda reference, manifest_digest, log=None:
            events.append(("signature", reference, manifest_digest)),
    )
    monkeypatch.setattr(
        convert.evidence_verification,
        "check",
        lambda reference, raw_manifest, client, manifest_fetch, log=None:
            events.append(("direct-evidence", reference)) or direct_result,
    )
    monkeypatch.setattr(convert, "cache_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        convert.evidence_verification,
        "verify_pulled",
        lambda directory, reference, tool_id, result, log=None:
            events.append(("pulled-content", directory, reference, tool_id, result)),
    )

    repo = f"biochef-plugins-jq.query@{digest}"
    convert.fetch_tool("jq.query-1", repo)
    convert.fetch_tool("jq.query-2", repo)

    assert [event[0] for event in events] == [
        "signature", "direct-evidence", "pulled-content",
        "signature", "direct-evidence", "pulled-content",
    ]
    assert all(event[1] == subject for event in events if event[0] == "direct-evidence")
    assert not registry.pulled


def test_evidence_failure_does_not_promote_the_staged_bundle(tmp_path, monkeypatch):
    """A bundle refused by its evidence must never become the shared cache."""
    files = {
        "bundle.json": b'{"bin":"tool"}',
        "tool": b"native tool",
    }
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "mediaType": "application/octet-stream",
                "annotations": {"org.opencontainers.image.title": name},
            }
            for name, content in files.items()
        ],
    }
    body = json.dumps(manifest, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    registry = _RawRegistry(body)

    def pull(target, outdir):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (Path(outdir) / name).write_bytes(content)

    registry.pull = pull
    cache = tmp_path / "cache"
    checked = []
    evidence = object()

    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", "registry.example.test")
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setattr(convert.signing, "check", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        convert.evidence_verification,
        "check",
        lambda *args, **kwargs: evidence,
    )

    def refuse(directory, reference, tool_id, result, log=None):
        checked.append(Path(directory))
        raise evidence_verification.EvidenceVerificationError("evidence mismatch")

    monkeypatch.setattr(convert.evidence_verification, "verify_pulled", refuse)

    with pytest.raises(
        evidence_verification.EvidenceVerificationError,
        match="evidence mismatch",
    ):
        convert.fetch_tool("tool-1", f"x@{digest}")

    assert checked == [cache / "tool.part"], (
        "pulled evidence was checked only after the bundle became the shared cache"
    )
    assert not (cache / "tool").exists(), "the refused bundle was promoted"
    assert not (cache / "tool.part").exists(), "the refused staging directory survived"


# --- the acceptance criterion itself ----------------------------------------

class _RefusingRegistry(_RawRegistry):
    """A registry whose blobs must never be asked for."""

    def __init__(self, body):
        super().__init__(body)
        self.pulled = False

    def pull(self, **kwargs):
        self.pulled = True
        raise AssertionError("the bundle was pulled despite a refused signature")


def test_strict_stops_a_bundle_before_it_is_ever_pulled(tmp_path, monkeypatch):
    """#14's acceptance, driven through fetch_tool rather than around it.

    Everything above tests signing.check in isolation, which proves what that
    function decides and nothing about whether the decision reaches the fetch.
    That is the gap this suite had: a verification function nobody wired up
    passes every unit test it has.

    The registry here raises if its blobs are requested, so a refusal that
    happened too late would fail rather than pass quietly.
    """
    cosign = _stub_cosign(tmp_path, 1, "no matching signatures")
    body = json.dumps({"layers": []}).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    registry = _RefusingRegistry(body)

    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", "reg.test")
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY",
                       _policy_file(tmp_path, registry_prefix="reg.test/plugins-"))
    monkeypatch.setenv("BIOCHEF_COSIGN", cosign)
    with pytest.raises(signing.SignatureError):
        convert.fetch_tool("samtools", f"plugins-samtools.view@{digest}")

    assert not registry.pulled, "the refusal came after the bundle was fetched"
    assert not (tmp_path / "cache").exists(), (
        "a cache directory was created for a bundle that was refused"
    )


def test_off_still_pulls_so_the_test_above_is_not_passing_for_the_wrong_reason(
        tmp_path, monkeypatch):
    """Guard against the refusal being someone else's error.

    If fetch_tool failed here for an unrelated reason -- a malformed manifest,
    a missing container -- the test above would pass without the signature check
    having done anything. With verification off, the same call must get far
    enough to reach the pull.
    """
    registry = _RefusingRegistry(json.dumps({"layers": []}).encode())
    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", "reg.test")
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "off")

    with pytest.raises(AssertionError, match="pulled despite"):
        convert.fetch_tool("samtools", "plugins-samtools.view:1.0")
    assert registry.pulled
