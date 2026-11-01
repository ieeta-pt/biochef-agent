"""The Agent authenticates evidence for an explicit digest before execution."""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import evidence_verification


SUBJECT_BYTES = b'{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
SUBJECT_DIGEST = "sha256:" + hashlib.sha256(SUBJECT_BYTES).hexdigest()
REPOSITORY = "registry.example.test/biochef-plugins-jq.query"
SUBJECT = f"{REPOSITORY}@{SUBJECT_DIGEST}"
PROVENANCE = b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}'
PROVENANCE_DIGEST = "sha256:" + hashlib.sha256(PROVENANCE).hexdigest()

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
COMMIT = "12" * 20


def _digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _statement(predicate_type, predicate, name=REPOSITORY, digest=SUBJECT_DIGEST):
    return {
        "_type": "https://in-toto.io/Statement/v0.1",
        "subject": [{"name": name, "digest": {"sha256": digest.removeprefix("sha256:")}}],
        "predicateType": predicate_type,
        "predicate": predicate,
    }


def _slsa_statement():
    source_uri = "git+https://github.com/ieeta-pt/biochef-recipes@refs/heads/master"
    return _statement(POLICY["slsa_predicate_type"], {
        "builder": {"id": POLICY["slsa_builder_id"]},
        "buildType": POLICY["slsa_build_type"],
        "metadata": {"completeness": {"parameters": True}},
        "invocation": {
            "configSource": {
                "uri": source_uri,
                "digest": {"sha1": COMMIT},
                "entryPoint": ".github/workflows/upload.yml",
            },
            "parameters": {"event_inputs": {"publish": "true"}},
            "environment": {
                "github_sha1": COMMIT,
                "github_event_payload": {"inputs": {"publish": "true"}},
            },
        },
        "materials": [{"uri": source_uri, "digest": {"sha1": COMMIT}}],
    })


def _envelope(statement):
    payload = base64.b64encode(
        json.dumps(statement, separators=(",", ":")).encode()
    ).decode()
    return json.dumps({
        "payloadType": "application/vnd.in-toto+json",
        "payload": payload,
        "signatures": [{"sig": "verified-by-cosign"}],
    })


def _material(tmp_path):
    binary = b"native jq"
    bundle = {
        "id": "jq.query",
        "version": "1.6-bc.1",
        "bin": "jq",
        "runtime": {
            "modes": ["native"],
            "native": {"digest": _digest(binary)},
        },
    }
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.6"}
    for name, value in {
        "bundle.json": json.dumps(bundle, sort_keys=True).encode(),
        "sbom.cdx.json": json.dumps(sbom, sort_keys=True).encode(),
        "jq": binary,
    }.items():
        (tmp_path / name).write_bytes(value)
    return sbom


def _attachment(subject_digest=SUBJECT_DIGEST):
    return {
        "schemaVersion": 2,
        "mediaType": evidence_verification.OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": evidence_verification.SLSA_BUNDLE_MEDIA_TYPE,
        "subject": {
            "mediaType": evidence_verification.OCI_MANIFEST_MEDIA_TYPE,
            "digest": subject_digest,
            "size": len(SUBJECT_BYTES),
        },
        "layers": [{
            "mediaType": evidence_verification.SLSA_BUNDLE_MEDIA_TYPE,
            "digest": PROVENANCE_DIGEST,
            "size": len(PROVENANCE),
        }],
    }


class _Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _Container:
    registry = "registry.example.test"
    api_prefix = "biochef-plugins-jq.query"

    def manifest_url(self, tag=None):
        return f"registry.example.test/v2/{self.api_prefix}/manifests/{tag}"


class _Registry:
    prefix = "https"

    def __init__(self, attachment=None, native_referrers=False):
        self.attachment = attachment or _attachment()
        self.attachment_bytes = json.dumps(
            self.attachment, separators=(",", ":")
        ).encode()
        self.attachment_digest = _digest(self.attachment_bytes)
        self.native_referrers = native_referrers
        self.requested_urls = []

    def get_container(self, target):
        return _Container()

    def get_blob(self, container, digest):
        assert digest == PROVENANCE_DIGEST
        return _Response(200, PROVENANCE)

    def _index(self):
        return json.dumps({
            "schemaVersion": 2,
            "mediaType": evidence_verification.OCI_INDEX_MEDIA_TYPE,
            "manifests": [{
                "mediaType": evidence_verification.OCI_MANIFEST_MEDIA_TYPE,
                "digest": self.attachment_digest,
                "size": len(self.attachment_bytes),
                "artifactType": evidence_verification.SLSA_BUNDLE_MEDIA_TYPE,
            }],
        }).encode()

    def do_request(self, url, method="GET", headers=None):
        self.requested_urls.append(url)
        if "/referrers/" in url and not self.native_referrers:
            return _Response(404)
        return _Response(200, self._index())

    def fetch_manifest(self, target):
        return self.attachment, self.attachment_digest, self.attachment_bytes


def _policy_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY))
    return str(path)


def _direct_check(tmp_path, monkeypatch, cyclonedx=None, slsa=None, attachment=None):
    sbom = _material(tmp_path)
    cyclonedx = cyclonedx or _statement(
        evidence_verification.CYCLONEDX_PREDICATE_TYPE,
        sbom,
        name="cosign-generated-name",
    )
    slsa = slsa or _slsa_statement()
    calls = []
    registry = _Registry(attachment=attachment)

    def run(binary, arguments, label, **kwargs):
        calls.append((binary, arguments, label))
        return _envelope(cyclonedx) if label == "Cosign" else json.dumps(slsa)

    monkeypatch.setattr(evidence_verification, "_run", run)
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    result = evidence_verification.check(
        SUBJECT,
        SUBJECT_BYTES,
        registry,
        registry.fetch_manifest,
    )
    return result, calls, registry


def test_direct_check_authenticates_both_evidence_types(tmp_path, monkeypatch):
    result, calls, registry = _direct_check(tmp_path, monkeypatch)
    assert result.slsa_statement["predicate"]["builder"]["id"] == POLICY["slsa_builder_id"]
    assert calls[0][2] == "Cosign"
    assert "--certificate-identity" in calls[0][1]
    assert calls[1][2] == "SLSA Verifier"
    assert "--source-branch" in calls[1][1]
    assert "--print-provenance" in calls[1][1]
    assert "/referrers/" in registry.requested_urls[0]
    assert "/manifests/sha256-" in registry.requested_urls[1]


def test_native_oci_referrers_do_not_need_the_fallback_tag(tmp_path, monkeypatch):
    _material(tmp_path)
    registry = _Registry(native_referrers=True)
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setattr(
        evidence_verification,
        "_run",
        lambda binary, arguments, label, **kwargs: (
            _envelope(_statement(
                evidence_verification.CYCLONEDX_PREDICATE_TYPE,
                {"bomFormat": "CycloneDX", "specVersion": "1.6"},
            ))
            if label == "Cosign" else json.dumps(_slsa_statement())
        ),
    )
    evidence_verification.check(SUBJECT, SUBJECT_BYTES, registry, registry.fetch_manifest)
    assert len(registry.requested_urls) == 1
    assert "/referrers/" in registry.requested_urls[0]


def test_cyclonedx_must_name_the_exact_subject_digest(tmp_path, monkeypatch):
    wrong = _statement(
        evidence_verification.CYCLONEDX_PREDICATE_TYPE,
        {"bomFormat": "CycloneDX"},
        digest="sha256:" + "00" * 32,
    )
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="CycloneDX"):
        _direct_check(tmp_path, monkeypatch, cyclonedx=wrong)


def test_verified_slsa_statement_must_match_builder(tmp_path, monkeypatch):
    statement = _slsa_statement()
    statement["predicate"]["builder"]["id"] = "https://example.invalid/builder"
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="satisfied policy"):
        _direct_check(tmp_path, monkeypatch, slsa=statement)


def test_slsa_attachment_must_name_the_exact_subject(tmp_path, monkeypatch):
    wrong_attachment = _attachment("sha256:" + "00" * 32)
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="satisfied policy"):
        _direct_check(tmp_path, monkeypatch, attachment=wrong_attachment)


def test_authenticated_predicates_are_bound_to_the_pulled_files(tmp_path, monkeypatch):
    result, _, _ = _direct_check(tmp_path, monkeypatch)
    assert evidence_verification.verify_pulled(
        tmp_path, SUBJECT, "jq.query", result
    ) is True

    (tmp_path / "sbom.cdx.json").write_text('{"bomFormat":"changed"}')
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="pulled SBOM"):
        evidence_verification.verify_pulled(tmp_path, SUBJECT, "jq.query", result)


def test_bundle_identity_must_match_the_requested_operation(tmp_path, monkeypatch):
    result, _, _ = _direct_check(tmp_path, monkeypatch)
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="requested operation"):
        evidence_verification.verify_pulled(tmp_path, SUBJECT, "other.operation", result)


def test_warn_reports_a_post_pull_failure_and_continues(tmp_path, monkeypatch):
    result, _, _ = _direct_check(tmp_path, monkeypatch)
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "warn")
    (tmp_path / "jq").write_bytes(b"changed")
    said = []
    assert evidence_verification.verify_pulled(
        tmp_path, SUBJECT, "jq.query", result, log=said.append
    ) is False
    assert said and "pulled evidence not verified" in said[0]


def test_strict_refuses_when_cosign_is_missing(tmp_path, monkeypatch):
    registry = _Registry()
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv("BIOCHEF_COSIGN", str(tmp_path / "absent-cosign"))
    with pytest.raises(evidence_verification.EvidenceVerificationError, match="not available"):
        evidence_verification.check(
            SUBJECT, SUBJECT_BYTES, registry, registry.fetch_manifest
        )


def test_strict_refuses_when_slsa_verifier_is_missing(tmp_path, monkeypatch):
    registry = _Registry()
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    monkeypatch.setenv("BIOCHEF_SIGNING_POLICY", _policy_file(tmp_path))
    monkeypatch.setenv(
        "BIOCHEF_SLSA_VERIFIER", str(tmp_path / "absent-slsa-verifier")
    )
    monkeypatch.setattr(
        evidence_verification,
        "_verify_cyclonedx",
        lambda reference, policy: (),
    )

    with pytest.raises(
        evidence_verification.EvidenceVerificationError,
        match="SLSA Verifier binary is not available",
    ):
        evidence_verification.check(
            SUBJECT, SUBJECT_BYTES, registry, registry.fetch_manifest
        )
