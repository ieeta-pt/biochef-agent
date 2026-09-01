"""Where a run's inputs come from (#12).

The same shape as runner.py and auth.py: an interface, providers selected by
name, and the part every provider shares written once. F1 (htsget) and F2 (DRS)
should be further providers here rather than rewrites.

A provider WRITES INTO the workspace; it does not return bytes. That is the
decision the rest of this file follows from, and it is deliberate. Returning
bytes would mean every input passing through memory whole -- which is what the
service does today, and precisely the ceiling D2 exists to lift. A provider given
the workspace can stream, copy, or hardlink, and the ones that cannot hold a
multi-gigabyte BAM in memory are exactly the ones that matter.

What a provider does NOT decide is which names are legitimate. That is settled
before any of them is asked, against the workflow itself, because a source
naming its own destination is how a fetch becomes a write to somewhere else.
"""

import hashlib
import json
import os
import urllib.parse
import urllib.request
from urllib.parse import quote


class DataSourceError(Exception):
    """An input could not be obtained. The client's problem, not the service's."""


class DataSource:
    """A way of getting one named input into a run's workspace."""

    name = "source"

    def fetch(self, ws, name: str, spec) -> None:
        """Put `name` into `ws`, from wherever this provider gets things.

        `spec` is whatever the client supplied for this input, and its shape is
        the provider's business -- bytes for an upload, a path for localpath, a
        URL and a range for htsget later.

        `name` has already been checked twice: for being a usable file name, and
        for being an input this workflow actually declares. A provider must not
        widen either.
        """
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class UploadSource(DataSource):
    """Bytes pushed from the browser. What the service has always done.

    Kept as a named provider rather than the absence of one, so that the
    default path is visible in the same place as the alternatives.
    """

    name = "upload"

    def fetch(self, ws, name: str, spec) -> None:
        if not isinstance(spec, (bytes, bytearray)):
            raise DataSourceError(
                f"{name!r}: the upload source expects bytes, got "
                f"{type(spec).__name__}"
            )
        ws.write_bytes(name, bytes(spec))


class LocalPathSource(DataSource):
    """A file already on the agent's host.

    The ordinary case inside a TRE, where the data is on the machine and the
    only thing it should not have to do is travel to where it already is.

    Confined to a root the operator sets, because the client chooses the path.
    Without that this is an arbitrary-file-read: a workflow naming
    /etc/shadow as an input would have it copied into a workspace and returned
    as a tool's output. The root is not configured by default, so the provider
    refuses to start rather than defaulting to somewhere plausible.
    """

    name = "localpath"

    def __init__(self, root: str = None):
        root = LOCAL_ROOT if root is None else root
        if not root:
            raise ValueError(
                "the localpath source needs BIOCHEF_LOCAL_ROOT set to the "
                "directory it may read from. Refusing to start rather than "
                "guessing at one, because the client chooses the path."
            )
        self.root = os.path.realpath(root)

    def describe(self) -> str:
        return f"{self.name} ({self.root})"

    def fetch(self, ws, name: str, spec) -> None:
        if not isinstance(spec, str) or not spec:
            raise DataSourceError(
                f"{name!r}: the localpath source expects a path, got "
                f"{type(spec).__name__}"
            )

        # Resolved and then checked against the root, rather than checked and
        # then resolved. A symlink inside the root pointing outside it passes
        # any test done on the path as written.
        resolved = os.path.realpath(os.path.join(self.root, spec))
        if resolved != self.root and not resolved.startswith(self.root + os.sep):
            raise DataSourceError(
                f"{name!r}: {spec!r} resolves outside BIOCHEF_LOCAL_ROOT"
            )

        if not os.path.isfile(resolved):
            raise DataSourceError(f"{name!r}: {spec!r} is not a file")

        # Copied through the workspace's own writer so the file lands with the
        # same O_EXCL and O_NOFOLLOW treatment as an upload, and streamed rather
        # than read whole -- the point of a provider writing into the workspace
        # instead of returning bytes.
        with open(resolved, "rb") as source:
            ws.write_stream(name, source)


class DrsSource(DataSource):
    """An object named by a GA4GH DRS URI (#21).

    `drs://host/id` resolves to `https://host/ga4gh/drs/v1/objects/id`, which is
    the whole difficulty: the URI names the host, and the URI comes from the
    client. Following one unchecked makes this service a request generator
    pointed wherever a workflow says -- from inside a TRE, at whatever that TRE
    can reach and the caller cannot.

    So the hosts are an allowlist and there is no default, exactly as
    LocalPathSource confines itself to a root because the client chooses the
    path. Unset disables the source rather than permitting everything.

    Compact identifiers (`drs://prefix:accession`) are refused. Resolving one
    means asking a third-party resolver which host to contact, and taking an
    endpoint out of a document and then trusting it completely is a mistake this
    codebase has already made twice.
    """

    name = "drs"

    # Everything the spec fixes, so none of it is a guess: DRS servers are at
    # https on 443 under this base path.
    BASE_PATH = "/ga4gh/drs/v1/objects"

    # Preferred first. sha-256 if the server offers it, md5 because most of them
    # only offer that. An object declaring neither is refused rather than taken
    # on trust -- an unverified download is the gap C1 exists to close, and it
    # does not stop being one because a different protocol opened it.
    CHECKSUMS = ("sha-256", "md5")

    def __init__(self, hosts=None, open_url=None):
        raw = DRS_HOSTS if hosts is None else hosts
        self.hosts = frozenset(
            part.strip().lower() for part in (raw or "").split(",") if part.strip()
        )
        if not self.hosts:
            raise ValueError(
                "the drs source needs BIOCHEF_DRS_HOSTS set to the DRS servers "
                "it may resolve against. Refusing to start rather than "
                "following whatever host a workflow names."
            )
        self._open_url = open_url or _open_url

    def describe(self) -> str:
        return f"{self.name} ({', '.join(sorted(self.hosts))})"

    def fetch(self, ws, name: str, spec) -> None:
        if not isinstance(spec, str) or not spec:
            raise DataSourceError(
                f"{name!r}: the drs source expects a drs:// URI, got "
                f"{type(spec).__name__}"
            )
        host, object_id = self._parse(name, spec)

        document = self._json(f"https://{host}{self.BASE_PATH}/{quote(object_id, safe='')}")
        url, headers = self._access(host, object_id, document)

        expected = self._checksum(name, document)
        declared_size = document.get("size")

        digest = hashlib.new("sha256" if expected[0] == "sha-256" else "md5")
        counted = _Counted(self._open_url(url, headers), digest, declared_size, name)
        ws.write_stream(name, counted)

        actual = digest.hexdigest()
        if actual.lower() != expected[1].lower():
            raise DataSourceError(
                f"{name!r}: {spec} arrived with {expected[0]} {actual}, but the "
                f"DRS object declares {expected[1]}. The bytes are not the ones "
                f"named."
            )

    def _parse(self, name, uri):
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "drs":
            raise DataSourceError(f"{name!r}: {uri!r} is not a drs:// URI")
        authority = parsed.netloc
        if ":" in authority:
            # The spec uses the colon to tell the two forms apart, and a port is
            # not a thing here: a conformant server is on 443.
            raise DataSourceError(
                f"{name!r}: {uri!r} looks like a compact identifier. Only "
                f"hostname-based DRS URIs are resolved, because resolving a "
                f"compact one means letting a third-party resolver choose which "
                f"host this service contacts."
            )
        host = authority.lower()
        if host not in self.hosts:
            raise DataSourceError(
                f"{name!r}: {host!r} is not in BIOCHEF_DRS_HOSTS"
            )
        object_id = parsed.path.lstrip("/")
        # The id becomes a path segment. One containing a slash or a dot segment
        # would reach somewhere else on that server entirely.
        if not object_id or "/" in object_id or object_id.startswith("."):
            raise DataSourceError(f"{name!r}: {uri!r} has no usable object id")
        return host, object_id

    def _access(self, host, object_id, document):
        """A URL to read the bytes from, following the spec's two shapes.

        An access_url may point at any host at all -- a presigned S3 or GCS URL
        is the normal case and the reason the allowlist covers the DRS server
        rather than the bytes. What protects the download is the checksum the
        object itself declares, which is checked after the fact.
        """
        methods = document.get("access_methods") or []
        if not isinstance(methods, list) or not methods:
            raise DataSourceError(f"{object_id}: the DRS object offers no access_methods")

        for method in methods:
            if not isinstance(method, dict):
                continue
            direct = method.get("access_url")
            if isinstance(direct, dict) and direct.get("url"):
                return direct["url"], _header_list(direct.get("headers"))

        for method in methods:
            if not isinstance(method, dict) or not method.get("access_id"):
                continue
            resolved = self._json(
                f"https://{host}{self.BASE_PATH}/{quote(object_id, safe='')}"
                f"/access/{quote(str(method['access_id']), safe='')}"
            )
            if isinstance(resolved, dict) and resolved.get("url"):
                return resolved["url"], _header_list(resolved.get("headers"))

        raise DataSourceError(
            f"{object_id}: no access_method yielded a URL to read from"
        )

    def _checksum(self, name, document):
        offered = document.get("checksums") or []
        by_type = {
            str(entry.get("type", "")).lower(): str(entry.get("checksum", ""))
            for entry in offered if isinstance(entry, dict)
        }
        for algorithm in self.CHECKSUMS:
            if by_type.get(algorithm):
                return algorithm, by_type[algorithm]
        raise DataSourceError(
            f"{name!r}: the DRS object declares no {' or '.join(self.CHECKSUMS)} "
            f"checksum, so what arrives cannot be checked against what was named"
        )

    def _json(self, url):
        with self._open_url(url, {}) as response:
            try:
                document = json.loads(response.read())
            except ValueError as exc:
                raise DataSourceError(f"{url} did not answer with JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise DataSourceError(
                f"{url} answered with {type(document).__name__}, not an object")
        return document


class _Counted:
    """A response body that hashes as it passes and refuses to overrun.

    A server declaring a small size and then sending without end would otherwise
    fill the disk, and the workspace writer has no reason to know what the DRS
    object claimed.
    """

    # Enough headroom for a server whose size is slightly stale, without letting
    # "declared 1KB" become "wrote 10GB".
    SLACK = 1024 * 1024

    def __init__(self, response, digest, declared_size, name):
        self._response = response
        self._digest = digest
        self._limit = None if declared_size is None else int(declared_size) + self.SLACK
        self._seen = 0
        self._name = name

    def read(self, size=-1):
        chunk = self._response.read(size)
        if chunk:
            self._seen += len(chunk)
            if self._limit is not None and self._seen > self._limit:
                raise DataSourceError(
                    f"{self._name!r}: the server sent more than the "
                    f"{self._limit - self.SLACK} bytes the DRS object declared"
                )
            self._digest.update(chunk)
        return chunk


def _header_list(headers):
    """DRS gives headers as a list of "Name: value" strings."""
    if not isinstance(headers, list):
        return {}
    out = {}
    for entry in headers:
        if isinstance(entry, str) and ":" in entry:
            key, _, value = entry.partition(":")
            out[key.strip()] = value.strip()
    return out


def _open_url(url, headers):
    if not url.lower().startswith("https://"):
        raise DataSourceError(f"refusing to read over an insecure URL: {url}")
    request = urllib.request.Request(url, headers=dict(headers or {}))
    return urllib.request.urlopen(request, timeout=60)


PROVIDERS = {
    UploadSource.name: UploadSource,
    LocalPathSource.name: LocalPathSource,
    DrsSource.name: DrsSource,
}

DRS_HOSTS = os.getenv("BIOCHEF_DRS_HOSTS", "")
"""The DRS servers this deployment may resolve against.

Empty by default, which disables the source. A DRS URI names its own host, and
the URI comes from the client, so a service that follows any of them is a
request generator aimed at whatever it can reach from inside the network it sits
in.
"""

LOCAL_ROOT = os.getenv("BIOCHEF_LOCAL_ROOT", "")
"""The only directory the localpath source may read from.

Empty by default, which disables that source entirely. A service that can be
told to read any path on its host is a file-read primitive with a workflow
engine attached.
"""

ENABLED = [
    part.strip() for part in
    os.getenv("BIOCHEF_DATA_SOURCES", UploadSource.name).split(",")
    if part.strip()
]
"""Which sources a deployment permits, most restrictive first in the docs.

Defaults to `upload` alone, so nothing changes for an existing deployment and
localpath is something an operator turns on knowingly.
"""


def get_sources(names=None):
    """Resolve the permitted providers, refusing to start on one that is absent.

    The same shape as runner.get_runner and auth.get_auth. A name that is not a
    provider stops the process, because a typo silently falling back would leave
    a deployment with sources it did not ask for or without ones it did.
    """
    names = ENABLED if names is None else names
    if not names:
        raise ValueError(
            "BIOCHEF_DATA_SOURCES is empty, so no input could ever be supplied."
        )
    resolved = {}
    for name in names:
        try:
            provider = PROVIDERS[name]
        except KeyError:
            raise ValueError(
                f"BIOCHEF_DATA_SOURCES names {name!r}, which is not a data "
                f"source. Available: {', '.join(sorted(PROVIDERS))}."
            ) from None
        resolved[name] = provider()
    return resolved
