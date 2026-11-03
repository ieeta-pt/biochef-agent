"""Verify a bundle's attestations and executable bytes before use.

The registry tells the Agent where evidence is stored, but the Agent accepts that evidence only
after Cosign or SLSA Verifier authenticates it and it names the exact OCI subject requested by the caller.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import signing


OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
SLSA_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
CYCLONEDX_PREDICATE_TYPE = "https://cyclonedx.org/bom"
IN_TOTO_TYPES = {
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
}
DEFAULT_TIMEOUT_SECONDS = 120
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class EvidenceVerificationError(Exception):
    """Evidence was absent, altered, or incompatible with local policy."""


@dataclass(frozen=True)
class EvidenceResult:
    cyclonedx_statements: tuple[dict, ...]
    slsa_statement: dict


def _sha256(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_object(value, label):
    try:
        document = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError(f"{label} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise EvidenceVerificationError(f"{label} must be a JSON object")
    return document


def _subject(reference):
    try:
        repository, digest = reference.rsplit("@", 1)
    except (AttributeError, ValueError) as exc:
        raise EvidenceVerificationError(
            "direct evidence verification requires an immutable OCI reference"
        ) from exc
    if not repository or not _DIGEST.fullmatch(digest):
        raise EvidenceVerificationError(
            "direct evidence verification requires an immutable OCI reference"
        )
    return repository, digest


def _run(binary, arguments, label, timeout=DEFAULT_TIMEOUT_SECONDS, environment=None):
    executable = shutil.which(binary) if os.path.sep not in binary else binary
    if not executable or not Path(executable).is_file():
        raise EvidenceVerificationError(f"{label} binary is not available: {binary}")
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceVerificationError(f"could not execute {label}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"{label} verification failed").strip()
        raise EvidenceVerificationError(detail[:1000])
    return result.stdout


def _attestation_statements(output):
    decoder = json.JSONDecoder()
    statements = []
    position = 0
    while position < len(output):
        while position < len(output) and output[position].isspace():
            position += 1
        if position == len(output):
            break
        try:
            envelope, position = decoder.raw_decode(output, position)
            payload = envelope.get("payload")
            if (
                envelope.get("payloadType") != "application/vnd.in-toto+json"
                or not isinstance(payload, str)
            ):
                raise EvidenceVerificationError("Cosign returned a malformed DSSE envelope")
            statement = json.loads(base64.b64decode(payload, validate=True))
        except (AttributeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EvidenceVerificationError("Cosign returned malformed attestation output") from exc
        if not isinstance(statement, dict):
            raise EvidenceVerificationError("Cosign attestation payload is not an object")
        statements.append(statement)
    if not statements:
        raise EvidenceVerificationError("Cosign returned no attestation statements")
    return statements


def _matches_subject(statement, predicate_type, reference, require_name=False):
    repository, digest = _subject(reference)
    if statement.get("_type") not in IN_TOTO_TYPES or statement.get("predicateType") != predicate_type:
        return False
    for subject in statement.get("subject") or []:
        if not isinstance(subject, dict):
            continue
        subject_digests = subject.get("digest")
        subject_digest = (
            subject_digests.get("sha256") if isinstance(subject_digests, dict) else None
        )
        if (
            isinstance(subject_digest, str)
            and subject_digest.lower() == digest.removeprefix("sha256:").lower()
            and (not require_name or subject.get("name") == repository)
        ):
            return True
    return False


def _sha1(document):
    if not isinstance(document, dict):
        return None
    digests = document.get("digest")
    return digests.get("sha1") if isinstance(digests, dict) else None


def _verify_cyclonedx(reference, policy):
    arguments = [
        "verify-attestation",
        "--type", "cyclonedx",
        "--certificate-identity", policy["certificate_identity"],
        "--certificate-oidc-issuer", policy["certificate_oidc_issuer"],
        reference,
    ]
    output = _run(
        os.getenv("BIOCHEF_COSIGN", "cosign"),
        arguments,
        "Cosign",
        environment={**os.environ, "COSIGN_YES": "true"},
    )
    statements = _attestation_statements(output)
    if not any(
        _matches_subject(item, CYCLONEDX_PREDICATE_TYPE, reference)
        for item in statements
    ):
        raise EvidenceVerificationError(
            "verified CycloneDX attestation does not match the requested subject"
        )
    return tuple(statements)


def _blob(client, target, digest, label):
    try:
        response = client.get_blob(client.get_container(target), digest)
    except Exception as exc:
        raise EvidenceVerificationError(f"{label} could not be fetched: {exc}") from exc
    if response.status_code != 200:
        raise EvidenceVerificationError(
            f"the registry answered {response.status_code} for {label}"
        )
    if _sha256(response.content) != digest:
        raise EvidenceVerificationError(f"{label} failed its OCI digest check")
    return response.content


def _source_arguments(policy):
    reference = policy["slsa_source_ref"]
    if reference.startswith("refs/heads/"):
        return ["--source-branch", reference.removeprefix("refs/heads/")]
    if reference.startswith("refs/tags/"):
        return ["--source-tag", reference.removeprefix("refs/tags/")]
    raise EvidenceVerificationError("SLSA source ref is neither a branch nor a tag")


def _validate_slsa_statement(statement, reference, policy):
    if not _matches_subject(
        statement, policy["slsa_predicate_type"], reference, require_name=True
    ):
        raise EvidenceVerificationError("official SLSA statement names a different subject")

    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise EvidenceVerificationError("official SLSA statement is malformed")
    invocation = predicate.get("invocation")
    if not isinstance(invocation, dict):
        raise EvidenceVerificationError("official SLSA invocation is malformed")
    config_source = invocation.get("configSource")
    environment = invocation.get("environment")
    if not isinstance(config_source, dict) or not isinstance(environment, dict):
        raise EvidenceVerificationError("official SLSA invocation is malformed")

    builder = predicate.get("builder")
    metadata = predicate.get("metadata")
    completeness = metadata.get("completeness") if isinstance(metadata, dict) else None
    parameters = invocation.get("parameters") or {}
    event_payload = environment.get("github_event_payload") or {}
    materials = predicate.get("materials")
    source_uri = f"git+https://{policy['slsa_source_repository']}@{policy['slsa_source_ref']}"
    workflow = policy["slsa_source_workflow"].split("@", 1)[0]
    repository = policy["slsa_source_repository"].removeprefix("github.com/")
    entrypoint = workflow.removeprefix(f"{repository}/")
    source_commit = _sha1(config_source)

    valid = (
        isinstance(builder, dict)
        and builder.get("id") == policy["slsa_builder_id"]
        and predicate.get("buildType") == policy["slsa_build_type"]
        and isinstance(completeness, dict)
        and completeness.get("parameters") is True
        and config_source.get("uri") == source_uri
        and isinstance(source_commit, str)
        and _COMMIT.fullmatch(source_commit)
        and config_source.get("entryPoint") == entrypoint
        and environment.get("github_sha1") == source_commit
        and isinstance(parameters, dict)
        and isinstance(event_payload, dict)
        and (parameters.get("event_inputs") or {}) == (event_payload.get("inputs") or {})
        and isinstance(materials, list)
        and any(
            isinstance(material, dict)
            and material.get("uri") == source_uri
            and _sha1(material) == source_commit
            for material in materials
        )
    )
    if not valid:
        raise EvidenceVerificationError(
            "official SLSA statement does not satisfy the local source/builder policy"
        )


def _referrer_descriptors(reference, client):
    """Discover SLSA attachments through OCI 1.1 or its referrers-tag fallback.

    Discovery metadata is untrusted.  Every returned descriptor is subsequently
    fetched by digest and its subject and authenticated provenance are checked.
    """
    repository, digest = _subject(reference)
    if not (hasattr(client, "do_request") and hasattr(client, "prefix")):
        raise EvidenceVerificationError("the registry client cannot discover OCI referrers")

    container = client.get_container(reference)
    headers = {"Accept": OCI_INDEX_MEDIA_TYPE}
    referrers_url = (
        f"{client.prefix}://{container.registry}/v2/{container.api_prefix}"
        f"/referrers/{digest}"
    )
    try:
        response = client.do_request(referrers_url, "GET", headers=headers)
    except Exception as exc:
        raise EvidenceVerificationError(f"SLSA referrers could not be discovered: {exc}") from exc

    if response.status_code == 404:
        fallback_tag = digest.replace(":", "-", 1)
        fallback_url = f"{client.prefix}://{container.manifest_url(fallback_tag)}"
        try:
            response = client.do_request(fallback_url, "GET", headers=headers)
        except Exception as exc:
            raise EvidenceVerificationError(
                f"SLSA referrers-tag index could not be fetched: {exc}"
            ) from exc
    if response.status_code != 200:
        raise EvidenceVerificationError(
            f"the registry answered {response.status_code} while discovering SLSA referrers"
        )

    index = _json_object(response.content, "OCI referrers index")
    if index.get("schemaVersion") != 2 or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE:
        raise EvidenceVerificationError("the OCI referrers response is not an OCI image index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise EvidenceVerificationError("the OCI referrers index has no manifest list")

    descriptors = []
    for descriptor in manifests:
        if not isinstance(descriptor, dict) or descriptor.get("artifactType") != SLSA_BUNDLE_MEDIA_TYPE:
            continue
        attachment_digest = descriptor.get("digest")
        size = descriptor.get("size")
        if (
            descriptor.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
            or not isinstance(attachment_digest, str)
            or not _DIGEST.fullmatch(attachment_digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
        ):
            raise EvidenceVerificationError("the SLSA referrer descriptor is malformed")
        descriptors.append((f"{repository}@{attachment_digest}", size))
    if not descriptors:
        raise EvidenceVerificationError("no official SLSA attachment refers to the subject")
    return descriptors


def _verify_slsa_candidate(
    reference,
    raw_subject_manifest,
    attachment_reference,
    expected_attachment_size,
    policy,
    client,
    manifest_fetch_raw,
):
    _, subject_digest = _subject(reference)
    try:
        attachment, resolved_digest, raw_attachment = manifest_fetch_raw(attachment_reference)
    except Exception as exc:
        raise EvidenceVerificationError(f"SLSA attachment manifest could not be fetched: {exc}") from exc
    _, attachment_digest = _subject(attachment_reference)
    if (
        resolved_digest != attachment_digest
        or raw_attachment is None
        or len(raw_attachment) != expected_attachment_size
    ):
        raise EvidenceVerificationError("SLSA attachment manifest digest or size is incorrect")
    if not isinstance(attachment, dict) or (
        attachment.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
        or attachment.get("artifactType") != SLSA_BUNDLE_MEDIA_TYPE
    ):
        raise EvidenceVerificationError("SLSA attachment has an invalid OCI artifact type")
    subject = attachment.get("subject")
    if not isinstance(subject, dict) or (
        subject.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
        or subject.get("digest") != subject_digest
        or subject.get("size") != len(raw_subject_manifest)
    ):
        raise EvidenceVerificationError("SLSA attachment descriptor names a different subject")
    layers = attachment.get("layers")
    if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], dict):
        raise EvidenceVerificationError("SLSA attachment must contain exactly one layer")
    layer = layers[0]
    layer_digest = layer.get("digest")
    layer_size = layer.get("size")
    if (
        layer.get("mediaType") != SLSA_BUNDLE_MEDIA_TYPE
        or not isinstance(layer_digest, str)
        or not _DIGEST.fullmatch(layer_digest)
        or not isinstance(layer_size, int)
        or isinstance(layer_size, bool)
        or layer_size < 1
    ):
        raise EvidenceVerificationError("SLSA provenance layer descriptor is malformed")
    provenance = _blob(client, attachment_reference, layer_digest, "SLSA provenance")
    if len(provenance) != layer_size:
        raise EvidenceVerificationError("SLSA provenance layer has the wrong size")

    with tempfile.TemporaryDirectory(prefix="biochef-slsa-verify-") as temporary_name:
        temporary = Path(temporary_name)
        subject_path = temporary / "subject.manifest.json"
        provenance_path = temporary / "provenance.bundle"
        subject_path.write_bytes(raw_subject_manifest)
        provenance_path.write_bytes(provenance)
        arguments = [
            "verify-artifact", str(subject_path),
            "--provenance-path", str(provenance_path),
            "--source-uri", policy["slsa_source_repository"],
            "--builder-id", policy["slsa_builder_id"],
            *_source_arguments(policy),
            "--print-provenance",
        ]
        output = _run(
            os.getenv("BIOCHEF_SLSA_VERIFIER", "slsa-verifier"),
            arguments,
            "SLSA Verifier",
        )
    statement = _json_object(output, "verified official SLSA statement")
    _validate_slsa_statement(statement, reference, policy)
    return statement


def _verify_slsa(reference, raw_subject_manifest, policy, client, manifest_fetch_raw):
    if raw_subject_manifest is None:
        raise EvidenceVerificationError("exact subject manifest bytes are unavailable")

    failures = []
    for attachment_reference, size in _referrer_descriptors(reference, client):
        try:
            return _verify_slsa_candidate(
                reference,
                raw_subject_manifest,
                attachment_reference,
                size,
                policy,
                client,
                manifest_fetch_raw,
            )
        except EvidenceVerificationError as exc:
            failures.append(str(exc))
    raise EvidenceVerificationError(
        "no discovered SLSA attachment satisfied policy: " + "; ".join(failures)
    )


def check(reference, raw_subject_manifest, client, manifest_fetch_raw, log=None):
    """Verify direct evidence under the same rollout mode as subject signing."""
    current = signing.mode()
    if current == signing.OFF:
        return None
    try:
        _subject(reference)
        policy = signing.load_policy(os.getenv("BIOCHEF_SIGNING_POLICY"))
        cyclonedx = _verify_cyclonedx(reference, policy)
        slsa = _verify_slsa(reference, raw_subject_manifest, policy, client, manifest_fetch_raw)
        return EvidenceResult(cyclonedx, slsa)
    except (EvidenceVerificationError, signing.SignatureError) as exc:
        if current == signing.STRICT:
            if isinstance(exc, EvidenceVerificationError):
                raise
            raise EvidenceVerificationError(str(exc)) from exc
        if log is not None:
            log(
                f"direct evidence not verified ({exc}); continuing because "
                f"BIOCHEF_SIGNING_MODE is {signing.WARN}"
            )
        return None


def _read_file(root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise EvidenceVerificationError(f"unsafe evidence path: {name!r}")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceVerificationError(f"unsafe evidence path: {name!r}")
    path = root.joinpath(relative)
    if path.is_symlink() or not path.is_file():
        raise EvidenceVerificationError(f"missing or unsafe evidence file: {name}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceVerificationError(f"evidence path escapes the bundle: {name}") from exc
    return path.read_bytes()


def _verify_pulled(directory, reference, expected_tool_id, result):
    root = Path(directory).resolve()
    sbom_bytes = _read_file(root, "sbom.cdx.json")
    sbom = _json_object(sbom_bytes, "sbom.cdx.json")
    if not any(
        _matches_subject(statement, CYCLONEDX_PREDICATE_TYPE, reference)
        and statement.get("predicate") == sbom
        for statement in result.cyclonedx_statements
    ):
        raise EvidenceVerificationError(
            "CycloneDX attestation predicate differs from the pulled SBOM"
        )

    bundle = _json_object(_read_file(root, "bundle.json"), "bundle.json")
    if bundle.get("id") != expected_tool_id:
        raise EvidenceVerificationError(
            f"bundle identity {bundle.get('id')!r} differs from requested operation "
            f"{expected_tool_id!r}"
        )
    runtime = bundle.get("runtime")
    if not isinstance(runtime, dict):
        raise EvidenceVerificationError("bundle has no verified native runtime")
    modes = runtime.get("modes")
    native = runtime.get("native")
    native_digest = native.get("digest") if isinstance(native, dict) else None
    if (
        not isinstance(modes, list)
        or "native" not in modes
        or not isinstance(native_digest, str)
        or not _DIGEST.fullmatch(native_digest)
    ):
        raise EvidenceVerificationError("bundle has no verified native runtime")
    binary = _read_file(root, bundle.get("bin"))
    if _sha256(binary) != native_digest:
        raise EvidenceVerificationError("native executable digest differs from bundle.json")


def verify_pulled(directory, reference, expected_tool_id, result, log=None):
    """Bind authenticated predicates to the exact files that will be used."""
    if result is None:
        return True
    try:
        _verify_pulled(directory, reference, expected_tool_id, result)
    except EvidenceVerificationError as exc:
        if signing.mode() == signing.STRICT:
            raise
        if log is not None:
            log(
                f"pulled evidence not verified ({exc}); continuing because "
                f"BIOCHEF_SIGNING_MODE is {signing.WARN}"
            )
        return False
    return True
