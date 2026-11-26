"""What a task may name: images, and places on this host.

Every refusal is a `Refused` with an HTTP status and a reason an operator can act
on. Nothing here follows a symbolic link and nothing here opens the node's
database: a path is accepted only if it is already what it says it is.
"""

import os
import urllib.parse
from typing import NamedTuple

from tesapi.config import Config, overlaps, within

WORKDIR = "/work"
"""Where a task says its files are. The command runs in the workspace, which this
is called, so `/work/x` and `x` are the same file."""

RESERVED = frozenset({"Snakefile", ".snakemake", ".biochef-done"})
"""Names in the workspace that belong to the agent, whatever a task asks."""


class Refused(Exception):
    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class Policy(NamedTuple):
    config: Config

    def image(self, image) -> str:
        if not isinstance(image, str) or image not in self.config.images:
            raise Refused(f"the image {str(image)[:120]!r} is not one this node runs; "
                          f"it runs {', '.join(self.config.images)}", 403)
        return image

    def url_path(self, url) -> str:
        """The absolute path a file:// URL names, or a refusal. Nothing else is read."""
        if not isinstance(url, str) or not url:
            raise Refused("a url is missing")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "file":
            raise Refused(f"{url[:80]!r}: only file:// URLs are accepted", 403)
        if parts.netloc not in ("", "localhost"):
            raise Refused("a file:// URL must name a path on this host")
        if parts.query or parts.fragment or ";" in parts.path:
            raise Refused("a file:// URL has no query, fragment or parameters")
        path = urllib.parse.unquote(parts.path)
        if (not path.startswith("/") or any(ord(c) < 32 or c == "\x7f" for c in path)
                or path != os.path.normpath(path)):
            raise Refused(f"{path[:120]!r} is not a normal absolute path")
        return path

    def _allowed(self, path, roots, what):
        for root in roots:
            if within(path, root):
                break
        else:
            raise Refused(f"{path} is not in a directory this node lets tasks {what}; "
                          f"they are {', '.join(roots)}", 403)
        for other in self.config.denied:
            if overlaps(path, other):
                raise Refused(f"{path} is where the node keeps its own data", 403)

    def source(self, url) -> str:
        """An input: it must exist, lie in an allowed directory, and be a plain path."""
        path = self.url_path(url)
        if not os.path.lexists(path):
            raise Refused(f"{path} does not exist", 404)
        if os.path.realpath(path) != path:
            raise Refused(f"{path} is, or lies behind, a symbolic link", 403)
        self._allowed(path, self.config.read_dirs, "read")
        return path

    def destination(self, url) -> str:
        """An output: its parent must exist, be allowed, and the target not a link."""
        path = self.url_path(url)
        parent = os.path.dirname(path)
        if os.path.realpath(parent) != parent or not os.path.isdir(parent):
            raise Refused(f"{parent} must be an existing directory, behind no symlink")
        if os.path.islink(path):
            raise Refused(f"{path} is a symbolic link", 403)
        self._allowed(path, self.config.write_dirs, "write")
        return path

    def workspace_path(self, path, directory=False) -> str:
        """A path inside the task, as a relative one in the workspace."""
        if not isinstance(path, str) or not (path == WORKDIR
                                             or path.startswith(WORKDIR + "/")):
            raise Refused(f"a task path must be {WORKDIR}/<file>, not {str(path)[:80]!r}")
        if path != os.path.normpath(path) or any(ord(c) < 32 for c in path):
            raise Refused(f"{path[:80]!r} is not a normal path")
        relative = path[len(WORKDIR):].lstrip("/")
        if not relative and not directory:
            raise Refused(f"{WORKDIR} itself can only be a directory")
        if relative.split("/")[0] in RESERVED:
            raise Refused(f"{relative.split('/')[0]!r} belongs to the agent", 403)
        return relative
