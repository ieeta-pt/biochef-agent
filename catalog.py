"""Resolve every tool through the signed catalog before anything is fetched.

A tag in a registry is a name anyone who can write to the registry can move.
The Hub publishes a catalog of exactly which manifest each package is, signed
with a key the web app pins, and the browser fetches nothing that is not in it.
This applies the same rule here: a workflow names a package, and the catalog --
not the registry's tag -- decides which manifest that is.

Two modes:

    required  every reference resolves through the catalog, or is refused.
              The default.
    off       references go to the registry as written, as before this
              existed. For a development registry that has no catalog.

The catalog fixes WHICH manifest runs; BIOCHEF_SIGNING_MODE then decides whether
that manifest must also verify again here. They meet in one place: under strict,
an exact digest the catalog no longer lists may still run, because signing,
rather than the catalog, vouches for it (#4).
"""

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.hashes import SHA256

import signing


REQUIRED = "required"
OFF = "off"
MODES = (REQUIRED, OFF)

CATALOG_SCHEMA = "biochef.verified-catalog.v1"
SIGNATURE_SCHEMA = "biochef.catalog-signature.v1"
CATALOG_MEDIA_TYPE = "application/vnd.biochef.verified-catalog+json"
SIGNATURE_MEDIA_TYPE = "application/vnd.biochef.catalog-signature+json"
CHANNEL = "latest"

DEFAULT_PACKAGE = "biochef-plugins-index"
DEFAULT_TTL_SECONDS = 300

# The key the web app pins in src/utils/catalogTrust.js. The same key, so the
# agent and the browser accept exactly the same catalogs.
DEFAULT_PUBLIC_JWK = {
    "alg": "ES256",
    "crv": "P-256",
    "kid": "biochef-catalog-key",
    "kty": "EC",
    "use": "sig",
    "x": "96FNjTbbBMsn0LpaFDTJFToaxfBsCZcCTKkEjNJi_js",
    "y": "mwG_hO5F6omiyrCD73nj4A4JCnaGheVHj9PuxpGHdf4",
}

SEQUENCE_FILE = ".catalog-sequence"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class CatalogError(signing.SignatureError):
    """A tool could not be resolved through a catalog this service trusts.

    A SignatureError, so it is refused with the same structured 403 as any
    other artifact that is not admitted.
    """


def mode():
    """Which mode this service is in. An unknown value is an error, not `off`."""
    value = os.getenv("BIOCHEF_TOOL_CATALOG", REQUIRED).strip().lower()
    if value not in MODES:
        raise CatalogError(
            f"BIOCHEF_TOOL_CATALOG is {value!r}, which is not one of "
            f"{', '.join(MODES)}"
        )
    return value


def _b64url_decode(value):
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CatalogError("a catalog value is not valid base64url") from exc


def public_key():
    """The catalog key: BIOCHEF_CATALOG_PUBLIC_JWK if set, else the pinned one."""
    configured = os.getenv("BIOCHEF_CATALOG_PUBLIC_JWK")
    if configured:
        try:
            jwk = json.loads(configured)
        except ValueError as exc:
            raise CatalogError("BIOCHEF_CATALOG_PUBLIC_JWK is not valid JSON") from exc
    else:
        jwk = DEFAULT_PUBLIC_JWK
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise CatalogError("the catalog public key must be an EC P-256 JWK")
    if "d" in jwk:
        raise CatalogError("the catalog public key must not contain a private key")
    if jwk.get("alg") not in (None, "ES256"):
        raise CatalogError("the catalog public key must be usable for ES256")
    for field in ("x", "y", "kid"):
        if not isinstance(jwk.get(field), str) or not jwk[field]:
            raise CatalogError(f"the catalog public key is missing {field}")
    x, y = _b64url_decode(jwk["x"]), _b64url_decode(jwk["y"])
    if len(x) != 32 or len(y) != 32:
        raise CatalogError("the catalog public key x and y must be 32 bytes")
    try:
        key = ec.EllipticCurvePublicNumbers(
            x=int.from_bytes(x, "big"), y=int.from_bytes(y, "big"),
            curve=ec.SECP256R1(),
        ).public_key()
    except ValueError as exc:
        raise CatalogError("the catalog public key is not a P-256 point") from exc
    return key, jwk["kid"]


def verify_catalog(catalog_bytes, signature_bytes, registry_url):
    """The catalog, if its signature verifies under the catalog key.

    The same checks the web app makes in verifySignedCatalog, in the same order:
    the signature document, the digest of the exact bytes, the key id, the
    ECDSA signature, and then the catalog's own fields.
    """
    try:
        document = json.loads(signature_bytes)
    except ValueError as exc:
        raise CatalogError("the catalog signature is not JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != SIGNATURE_SCHEMA:
        raise CatalogError("the catalog signature schema is not supported")
    if document.get("alg") != "ES256":
        raise CatalogError("the catalog signature algorithm is not supported")
    if document.get("signed_media_type") != CATALOG_MEDIA_TYPE:
        raise CatalogError("the catalog signature names another media type")
    signed_digest = document.get("signed_digest")
    actual = "sha256:" + hashlib.sha256(catalog_bytes).hexdigest()
    if not isinstance(signed_digest, str) or signed_digest.lower() != actual:
        raise CatalogError("the catalog bytes do not match the signed digest")

    key, kid = public_key()
    if document.get("keyid") != kid:
        raise CatalogError("the catalog was signed with a key this service does not pin")
    encoded = document.get("signature")
    if not isinstance(encoded, str):
        raise CatalogError("the catalog signature is missing")
    signature = _b64url_decode(encoded)
    if len(signature) != 64:
        raise CatalogError("the catalog signature has the wrong length")
    try:
        key.verify(
            utils.encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            ),
            catalog_bytes,
            ec.ECDSA(SHA256()),
        )
    except InvalidSignature as exc:
        raise CatalogError("the catalog signature does not verify") from exc

    try:
        catalog = json.loads(catalog_bytes)
    except ValueError as exc:
        raise CatalogError("the catalog is not JSON") from exc
    _validate(catalog, registry_url)
    return catalog


def _validate(catalog, registry_url):
    if not isinstance(catalog, dict) or catalog.get("schema") != CATALOG_SCHEMA:
        raise CatalogError("the catalog schema is not supported")
    if catalog.get("registry") != registry_url:
        raise CatalogError(
            f"the catalog is for {catalog.get('registry')!r}, not {registry_url!r}"
        )
    if catalog.get("channel") != CHANNEL:
        raise CatalogError("the catalog is not the latest channel")
    sequence = catalog.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise CatalogError("the catalog sequence is invalid")
    if not isinstance(catalog.get("packages"), dict):
        raise CatalogError("the catalog has no packages")


def _entry(catalog, package, registry_url):
    entry = catalog["packages"].get(package)
    if not isinstance(entry, dict):
        raise CatalogError(f"{package} is not in the signed catalog")
    digest = entry.get("manifest_digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise CatalogError(f"{package}: the catalog entry has no manifest digest")
    if entry.get("digest_reference") != f"{registry_url}/{package}@{digest}":
        raise CatalogError(f"{package}: the catalog entry's reference does not match its digest")
    if not isinstance(entry.get("version"), str) or not entry["version"]:
        raise CatalogError(f"{package}: the catalog entry has no version")
    verification = entry.get("verification")
    if not isinstance(verification, dict) or any(
        verification.get(field) != "passed"
        for field in ("status", "cosign_signature", "cyclonedx_attestation", "slsa_provenance")
    ):
        raise CatalogError(f"{package}: the catalog does not record this package as verified")
    return entry


def _split(repo):
    """(repository, tag, digest) from what a workflow node names."""
    repository, digest = (repo.split("@", 1) + [None])[:2]
    tag = None
    if ":" in repository.rsplit("/", 1)[-1]:
        repository, tag = repository.rsplit(":", 1)
    if not repository:
        raise CatalogError(f"{repo!r} names no repository")
    return repository, tag, digest


class Catalog:
    """The current signed catalog of one registry, refreshed every TTL.

    Its sequence is kept in a file next to the tool cache, so a catalog older
    than one this service has already accepted is refused across restarts too,
    not only within one process.
    """

    def __init__(self, registry_url, fetch_manifest, fetch_blob, state_dir,
                 package=None, ttl=None, clock=time.monotonic):
        self.registry_url = registry_url
        self.package = package or os.getenv("BIOCHEF_CATALOG_PACKAGE", DEFAULT_PACKAGE)
        self.ttl = DEFAULT_TTL_SECONDS if ttl is None else ttl
        self._fetch_manifest = fetch_manifest
        self._fetch_blob = fetch_blob
        self._state = os.path.join(state_dir, SEQUENCE_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._catalog = None
        self._fetched_at = None

    def pin(self, repo):
        """`repository@digest` for what the node names, decided by the catalog.

        A bare repository, `latest` and the catalog's version tag all resolve
        to the catalog's digest. Any other tag is refused, and so is any other
        digest unless signing is strict: the catalog lists only the current
        version, so a workflow that names an older one by digest could never be
        run twice (#4). Strict has to verify that exact manifest's signature,
        SBOM and provenance before anything runs. Under any other mode nothing
        would vouch for it.
        """
        repository, tag, digest = _split(repo)
        catalog = self.current()
        entry = _entry(catalog, repository, self.registry_url)
        expected = entry["manifest_digest"]
        if tag is not None and tag not in (CHANNEL, entry["version"]):
            raise CatalogError(
                f"{repo}: the signed catalog lists {repository} at version "
                f"{entry['version']}, not {tag}"
            )
        if digest is not None and digest != expected:
            if signing.mode() == signing.STRICT and _DIGEST.fullmatch(digest):
                return f"{repository}@{digest}"
            raise CatalogError(
                f"{repo}: the signed catalog lists {repository} as {expected}"
            )
        return f"{repository}@{expected}"

    def current(self):
        with self._lock:
            now = self._clock()
            if self._catalog is None or now - self._fetched_at >= self.ttl:
                catalog = self._load()
                self._accept_sequence(catalog["sequence"])
                self._catalog, self._fetched_at = catalog, now
            return self._catalog

    def _load(self):
        target = f"{self.registry_url}/{self.package}:{CHANNEL}"
        manifest, _, _ = self._fetch_manifest(target)
        layers = {
            (layer.get("annotations") or {}).get("org.opencontainers.image.title"): layer
            for layer in (manifest or {}).get("layers") or []
        }
        catalog_layer = layers.get("index.json")
        signature_layer = layers.get("index.sig.json")
        if not catalog_layer or catalog_layer.get("mediaType") != CATALOG_MEDIA_TYPE:
            raise CatalogError(f"{target}: no catalog layer")
        if not signature_layer or signature_layer.get("mediaType") != SIGNATURE_MEDIA_TYPE:
            raise CatalogError(f"{target}: no catalog signature layer")
        return verify_catalog(
            self._blob(target, catalog_layer),
            self._blob(target, signature_layer),
            self.registry_url,
        )

    def _blob(self, target, descriptor):
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise CatalogError(f"{target}: a catalog layer has no sha256 digest")
        content = self._fetch_blob(target, digest)
        if ("sha256:" + hashlib.sha256(content).hexdigest() != digest
                or len(content) != descriptor.get("size")):
            raise CatalogError(f"{target}: a catalog layer does not match its descriptor")
        return content

    def _accept_sequence(self, sequence):
        previous = 0
        try:
            with open(self._state, "r") as handle:
                previous = int(handle.read().strip() or 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            raise CatalogError(f"the recorded catalog sequence could not be read: {exc}") from exc
        if sequence < previous:
            raise CatalogError(
                f"the registry served catalog sequence {sequence}, older than "
                f"{previous} which this service has already accepted"
            )
        if sequence > previous:
            os.makedirs(os.path.dirname(self._state), exist_ok=True)
            partial = f"{self._state}.{os.getpid()}.part"
            with open(partial, "w") as handle:
                handle.write(f"{sequence}\n")
            os.replace(partial, self._state)
