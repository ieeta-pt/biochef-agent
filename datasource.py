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

import os


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


PROVIDERS = {
    UploadSource.name: UploadSource,
    LocalPathSource.name: LocalPathSource,
}

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
