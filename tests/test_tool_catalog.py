"""Which manifest a workflow's tool name runs is decided by the signed catalog.

Before this, a node naming `biochef-plugins-jq.query` reached the registry as
that name, which the registry answers with whatever `latest` points at. Anyone
who can write to the registry can repoint `latest`, and with signing `off` --
the default -- the agent then ran the binary from that manifest on the host.

The browser never had this problem: it reads the Hub's signed catalog and
fetches only the digests listed there. These tests pin the same rule for the
agent, against a registry whose tags have been moved.
"""

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.hashes import SHA256

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog  # noqa: E402
import convert  # noqa: E402
import main  # noqa: E402
import signing  # noqa: E402

pytestmark = pytest.mark.catalog

REGISTRY = "registry.example.test"
PACKAGE = "biochef-plugins-jq.query"


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Key:
    def __init__(self, kid="test-catalog-key"):
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": kid,
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }

    def sign(self, data):
        r, s = utils.decode_dss_signature(self.private.sign(data, ec.ECDSA(SHA256())))
        return {
            "schema": catalog.SIGNATURE_SCHEMA,
            "alg": "ES256",
            "keyid": self.jwk["kid"],
            "signed_media_type": catalog.CATALOG_MEDIA_TYPE,
            "signed_digest": _digest(data),
            "signature": _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
        }


def _entry(package, digest, version="1.7.1-bc.1", **verification):
    checks = {"status": "passed", "cosign_signature": "passed",
              "cyclonedx_attestation": "passed", "slsa_provenance": "passed"}
    checks.update(verification)
    return {
        "id": package.removeprefix("biochef-plugins-"),
        "package": package,
        "version": version,
        "manifest_digest": digest,
        "digest_reference": f"{REGISTRY}/{package}@{digest}",
        "verification": checks,
    }


class _Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status
        self.headers = {}


class _Registry:
    """Manifests by repository and reference, blobs by digest.

    Records every manifest reference asked for, which is what the tests look at:
    a tag reaching the registry is the thing that must not happen.
    """

    prefix = "https"

    def __init__(self):
        self.manifests = {}
        self.blobs = {}
        self.asked = []

    def put_manifest(self, repository, document, *tags):
        body = json.dumps(document, sort_keys=True).encode()
        digest = _digest(body)
        for reference in (digest, *tags):
            self.manifests[(repository, reference)] = body
        return digest

    def put_blob(self, data):
        self.blobs[_digest(data)] = data
        return {"digest": _digest(data), "size": len(data)}

    def get_container(self, target):
        path = target.split("/", 1)[1]
        if "@" in path:
            repository, reference = path.split("@", 1)
            digest = reference
        else:
            repository, reference = (path.rsplit(":", 1) + ["latest"])[:2]
            digest = None

        class _Container:
            pass

        container = _Container()
        container.repository, container.reference, container.digest = repository, reference, digest
        container.manifest_url = lambda: f"{REGISTRY}/v2/{repository}/manifests/{reference}"
        return container

    def do_request(self, url, method="GET", headers=None):
        path = url.split("/v2/", 1)[1]
        repository, reference = path.split("/manifests/", 1)
        self.asked.append(f"{repository}:{reference}" if not reference.startswith("sha256:")
                          else f"{repository}@{reference}")
        body = self.manifests.get((repository, reference))
        return _Response(body or b"", 200 if body else 404)

    def get_blob(self, container, digest):
        data = self.blobs.get(digest)
        return _Response(data or b"", 200 if data is not None else 404)

    def pull(self, **kwargs):
        raise AssertionError("these tests stop before any pull")


def _publish_catalog(registry, key, packages, sequence=10, registry_url=REGISTRY,
                     tamper=False):
    document = {
        "schema": catalog.CATALOG_SCHEMA,
        "channel": "latest",
        "version": f"catalog-{sequence}",
        "sequence": sequence,
        "registry": registry_url,
        "package_prefix": "biochef-plugins-",
        "packages": packages,
    }
    catalog_bytes = json.dumps(document, sort_keys=True).encode()
    signature = json.dumps(key.sign(catalog_bytes)).encode()
    if tamper:
        catalog_bytes = catalog_bytes.replace(b'"sequence": ', b'"sequence":  ')
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {**registry.put_blob(catalog_bytes), "mediaType": catalog.CATALOG_MEDIA_TYPE,
             "annotations": {"org.opencontainers.image.title": "index.json"}},
            {**registry.put_blob(signature), "mediaType": catalog.SIGNATURE_MEDIA_TYPE,
             "annotations": {"org.opencontainers.image.title": "index.sig.json"}},
        ],
    }
    registry.put_manifest("biochef-plugins-index", manifest, "latest")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A registry whose `latest` tag for the tool has been moved.

    The catalog lists the genuine manifest; the tag points at another one. The
    agent must ask for the genuine one by digest and never ask for the tag.
    """
    key = _Key()
    registry = _Registry()
    genuine = registry.put_manifest(PACKAGE, {"schemaVersion": 2, "layers": [], "n": "genuine"}, "1.7.1-bc.1")
    moved = registry.put_manifest(PACKAGE, {"schemaVersion": 2, "layers": [], "n": "moved"}, "latest")
    _publish_catalog(registry, key, {PACKAGE: _entry(PACKAGE, genuine)})

    monkeypatch.delenv("BIOCHEF_TOOL_CATALOG", raising=False)
    monkeypatch.setenv("BIOCHEF_CATALOG_PUBLIC_JWK", json.dumps(key.jwk))
    monkeypatch.setattr(convert, "client", registry)
    monkeypatch.setattr(convert, "REGISTRY_URL", REGISTRY)
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "_catalogs", {})
    return {"key": key, "registry": registry, "genuine": genuine, "moved": moved,
            "tmp": tmp_path}


class _Stop(Exception):
    pass


def _fetch(monkeypatch, repo):
    """fetch_tool up to the point the manifest is in hand, and what it fetched."""
    fetched = []
    real = convert.fetch_manifest

    def recording(target):
        if "/biochef-plugins-index" in target:
            return real(target)
        fetched.append(target)
        real(target)
        raise _Stop

    monkeypatch.setattr(convert, "fetch_manifest", recording)
    with pytest.raises(_Stop):
        convert.fetch_tool("jq.query-1", repo)
    return fetched


# --- the attack this exists for ---------------------------------------------

def test_the_default_is_to_resolve_through_the_catalog(monkeypatch):
    monkeypatch.delenv("BIOCHEF_TOOL_CATALOG", raising=False)
    assert catalog.mode() == catalog.REQUIRED


def test_a_moved_latest_tag_is_never_asked_for(world, monkeypatch):
    fetched = _fetch(monkeypatch, PACKAGE)

    assert fetched == [f"{REGISTRY}/{PACKAGE}@{world['genuine']}"]
    assert f"{PACKAGE}:latest" not in world["registry"].asked
    assert f"{PACKAGE}@{world['moved']}" not in world["registry"].asked


@pytest.mark.parametrize("suffix", [":latest", ":1.7.1-bc.1"])
def test_latest_and_the_catalog_version_mean_the_catalog_digest(world, monkeypatch, suffix):
    assert _fetch(monkeypatch, PACKAGE + suffix) == [
        f"{REGISTRY}/{PACKAGE}@{world['genuine']}"
    ]


def test_the_catalog_digest_named_explicitly_is_accepted(world, monkeypatch):
    assert _fetch(monkeypatch, f"{PACKAGE}@{world['genuine']}") == [
        f"{REGISTRY}/{PACKAGE}@{world['genuine']}"
    ]


# --- what is refused, and that it is refused before any tool manifest -------

@pytest.mark.parametrize("repo, reason", [
    (f"{PACKAGE}:1.6.0-bc.1", "not 1.6.0-bc.1"),
    ("biochef-plugins-not.listed", "not in the signed catalog"),
])
def test_a_reference_the_catalog_does_not_list_is_refused(world, repo, reason):
    with pytest.raises(catalog.CatalogError, match=reason):
        convert.fetch_tool("jq.query-1", repo)
    assert all(asked.startswith("biochef-plugins-index") for asked in world["registry"].asked)


def test_a_digest_other_than_the_catalogs_is_refused(world):
    with pytest.raises(catalog.CatalogError, match="lists biochef-plugins-jq.query as"):
        convert.fetch_tool("jq.query-1", f"{PACKAGE}@{world['moved']}")
    assert f"{PACKAGE}@{world['moved']}" not in world["registry"].asked


def test_a_refusal_is_the_same_403_as_any_artifact_not_admitted():
    assert issubclass(catalog.CatalogError, signing.SignatureError)
    assert signing.SignatureError in main.app.exception_handlers


# --- the catalog itself -----------------------------------------------------

def test_a_catalog_altered_after_signing_is_refused(world):
    _publish_catalog(world["registry"], world["key"],
                     {PACKAGE: _entry(PACKAGE, world["genuine"])}, tamper=True)
    with pytest.raises(catalog.CatalogError, match="do not match the signed digest"):
        convert.fetch_tool("jq.query-1", PACKAGE)


def test_a_catalog_signed_by_another_key_is_refused(world):
    _publish_catalog(world["registry"], _Key(kid="someone-else"),
                     {PACKAGE: _entry(PACKAGE, world["moved"])})
    with pytest.raises(catalog.CatalogError, match="key this service does not pin"):
        convert.fetch_tool("jq.query-1", PACKAGE)


def test_a_key_reusing_the_pinned_key_id_is_still_refused(world):
    _publish_catalog(world["registry"], _Key(kid=world["key"].jwk["kid"]),
                     {PACKAGE: _entry(PACKAGE, world["moved"])})
    with pytest.raises(catalog.CatalogError, match="does not verify"):
        convert.fetch_tool("jq.query-1", PACKAGE)


def test_the_pinned_key_is_the_one_the_web_app_pins(monkeypatch):
    monkeypatch.delenv("BIOCHEF_CATALOG_PUBLIC_JWK", raising=False)
    _, kid = catalog.public_key()
    assert kid == "biochef-catalog-key"
    assert catalog.DEFAULT_PUBLIC_JWK["x"] == "96FNjTbbBMsn0LpaFDTJFToaxfBsCZcCTKkEjNJi_js"
    assert catalog.DEFAULT_PUBLIC_JWK["y"] == "mwG_hO5F6omiyrCD73nj4A4JCnaGheVHj9PuxpGHdf4"


def test_a_catalog_for_another_registry_is_refused(world):
    _publish_catalog(world["registry"], world["key"],
                     {PACKAGE: _entry(PACKAGE, world["genuine"])},
                     registry_url="registry.elsewhere.test")
    with pytest.raises(catalog.CatalogError, match="registry.elsewhere.test"):
        convert.fetch_tool("jq.query-1", PACKAGE)


def test_a_package_the_catalog_does_not_record_as_verified_is_refused(world):
    _publish_catalog(world["registry"], world["key"],
                     {PACKAGE: _entry(PACKAGE, world["genuine"], slsa_provenance="failed")})
    with pytest.raises(catalog.CatalogError, match="not record this package as verified"):
        convert.fetch_tool("jq.query-1", PACKAGE)


def test_an_older_catalog_is_refused_even_after_a_restart(world, monkeypatch):
    """Rolling `latest` back to an older, validly signed catalog would bring back
    whatever that catalog listed. The sequence is recorded on disk, so a fresh
    process -- here, a fresh Catalog -- refuses it too."""
    entry = {PACKAGE: _entry(PACKAGE, world["genuine"])}
    _publish_catalog(world["registry"], world["key"], entry, sequence=20)
    _fetch(monkeypatch, PACKAGE)

    monkeypatch.setattr(convert, "_catalogs", {})
    _publish_catalog(world["registry"], world["key"], entry, sequence=19)
    with pytest.raises(catalog.CatalogError, match="older than 20"):
        convert.fetch_tool("jq.query-1", PACKAGE)
    recorded = Path(world["tmp"]) / "cache" / catalog.SEQUENCE_FILE
    assert recorded.read_text().strip() == "20"


def test_the_catalog_is_fetched_once_per_refresh_interval(world):
    now = [0.0]
    registry = world["registry"]
    pinned = catalog.Catalog(
        REGISTRY,
        convert.fetch_manifest,
        convert._fetch_catalog_blob,
        str(world["tmp"] / "state"),
        ttl=300,
        clock=lambda: now[0],
    )
    for _ in range(3):
        pinned.pin(PACKAGE)
    assert registry.asked.count("biochef-plugins-index:latest") == 1

    now[0] = 300.0
    pinned.pin(PACKAGE)
    assert registry.asked.count("biochef-plugins-index:latest") == 2


# --- modes ------------------------------------------------------------------

def test_off_sends_the_reference_as_written(world, monkeypatch):
    monkeypatch.setenv("BIOCHEF_TOOL_CATALOG", "off")
    assert _fetch(monkeypatch, PACKAGE) == [f"{REGISTRY}/{PACKAGE}"]
    assert "biochef-plugins-index:latest" not in world["registry"].asked


def test_an_unknown_mode_is_an_error_not_off(monkeypatch):
    monkeypatch.setenv("BIOCHEF_TOOL_CATALOG", "requried")
    with pytest.raises(catalog.CatalogError, match="requried"):
        catalog.mode()


def test_strict_accepts_a_bare_name_once_the_catalog_has_pinned_it(world, monkeypatch):
    """Strict refuses anything but a digest. Workflows from the web app name a
    package and nothing else, so until now they could not run under strict at
    all; resolved through the catalog, they reach the signature check."""
    monkeypatch.setenv("BIOCHEF_SIGNING_MODE", "strict")
    checked = []

    def stop(reference, digest, log=None):
        checked.append((reference, digest))
        raise _Stop

    monkeypatch.setattr(convert.signing, "check", stop)
    with pytest.raises(_Stop):
        convert.fetch_tool("jq.query-1", PACKAGE)
    assert checked == [(f"{REGISTRY}/{PACKAGE}@{world['genuine']}", world["genuine"])]
