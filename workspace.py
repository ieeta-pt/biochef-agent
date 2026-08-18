"""The names a run is allowed to use for a file.

A workflow's filenames are not user data. The converter generates every one of
them -- `convert.py` builds `f"{source}-{source_handle}"` -- so a name arriving
on an upload is a claim about which of those it is, and the only sensible
question is whether it is one.

This module answers the narrow half of that: whether an UPLOADED name is a
single, plain path component at all. Checking it against the specific set the
workflow declares is the other half and belongs with the run directory work
(#40), where the declared set is available. A name like "samtools" is a
perfectly good single component, so the shape rule passes it, and only the
declared set would catch that it collides with a tool binary.

What this does NOT do, and an earlier version of this docstring wrongly claimed
it did: it does not protect against a hostile node id. The converter builds
generated names as f"{source}-{source_handle}" from client-supplied edge JSON
and writes them into the Snakefile itself. Nothing here sees them -- check_name
is applied to uploads, and to a generated name only when it is read back, which
is after snakemake has already run. A node id is a separate hole, closed by
giving the Snakefile's own string literals the same treatment (the quoting PR),
not by this rule.

Stated plainly because the wrong version of it read as though a run were
contained against hostile workflow JSON, and it is not.
"""

import errno
import os
import re
import shutil
import tempfile

SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
"""One plain path component, and nothing that reads as an option or a dotfile.

`\\A` and `\\Z` rather than `^` and `$`. With `re.match`, `$` also matches just
before a trailing newline, so `^[A-Za-z0-9._-]+$` accepts `"evil-out\\n"` -- and
a newline in a name is exactly what would write an extra line into the generated
Snakefile. `\\Z` has no such exception.

The first character is restricted separately so a name cannot begin with `-`,
which a tool would read as a flag, or with `.`, which hides the file and admits
`.` and `..`.

128 characters is comfortably above every generated name -- the longest in the
catalogue is well under half that -- and below any filesystem limit.
"""


class UnsafeName(ValueError):
    """A client-supplied name that cannot be used as a file name."""


def check_name(name):
    """Return `name` if it is usable as a file name, else raise `UnsafeName`.

    Deliberately a rejection rather than a sanitisation. Stripping a name into
    safety would silently rename the client's file, and the workflow refers to
    it by the name that was sent -- so the run would then fail somewhere less
    obvious, with a message about a missing input rather than about the name.
    """
    if not isinstance(name, str):
        # starlette types UploadFile.filename as `str | None`, so this is a
        # value a client can actually produce, not a defensive flourish.
        raise UnsafeName(f"expected a name, got {type(name).__name__}")
    if not SAFE_NAME.match(name):
        raise UnsafeName(f"{name!r} is not a plain file name")
    return name


class Workspace:
    """A directory belonging to one run, and the only place that run may write.

    Every path is opened relative to a descriptor held open on the directory for
    the run's lifetime, rather than by building a string and hoping it stays
    inside. That is what makes containment structural: `check_name` guarantees a
    single path component, so there is no `/` for a resolved path to escape
    through, and `O_NOFOLLOW` refuses a symlink planted in a slot.

    The descriptor is opened once. Re-resolving the directory's path on every
    call would reintroduce the race the descriptor exists to avoid -- the
    directory could be moved or replaced between two operations of the same run.
    """

    def __init__(self, path: str):
        self.path = os.path.realpath(path)
        self._fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        check_name(name)
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                         dir_fd=self._fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                # A slot that is a symlink. On the read path this is the
                # exfiltration in #41: the target's contents would otherwise be
                # read and returned to the client.
                raise UnsafeName(f"{name!r} is a symbolic link") from None
            raise

        # And the same attack by hard link, which O_NOFOLLOW does not stop
        # because a hard link is not a symbolic link -- it is another name for
        # the same inode, indistinguishable from the original.
        #
        # A file this workspace created has exactly one link. Anything with more
        # was linked from somewhere else, which for a slot the agent is about to
        # read means the contents of a file outside the run would be returned to
        # the caller.
        try:
            if os.fstat(fd).st_nlink > 1:
                os.close(fd)
                raise UnsafeName(f"{name!r} has another name outside this run")
        except OSError:
            os.close(fd)
            raise
        return fd

    def open_write(self, name: str, *, exclusive: bool = True):
        """Open a slot for writing.

        Exclusive by default, so an upload cannot land on a name the run has
        already created -- a tool binary, the Snakefile, another upload. Note
        that O_EXCL fires before O_NOFOLLOW, so a symlinked slot is refused here
        as FileExistsError rather than as UnsafeName. Both refusals are correct;
        a caller reporting them to a client should map both.
        """
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
        return os.fdopen(self._open(name, flags), "wb")

    def open_read(self, name: str):
        return os.fdopen(self._open(name, os.O_RDONLY), "rb")

    def write_bytes(self, name: str, data: bytes, *, exclusive: bool = True) -> int:
        with self.open_write(name, exclusive=exclusive) as fh:
            return fh.write(data)

    def place_executable(self, source_path: str, name: str) -> str:
        """Copy a tool binary in and make it executable.

        Copied rather than linked so a run cannot alter the cached copy, and
        written through the same descriptor as everything else.
        """
        with open(source_path, "rb") as src:
            self.write_bytes(name, src.read())
        os.chmod(name, 0o700, dir_fd=self._fd)
        return os.path.join(self.path, name)

    def cleanup(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
        shutil.rmtree(self.path, ignore_errors=True)


def make_workspace(root: str = None) -> Workspace:
    """A fresh directory for one run, readable only by this user."""
    if root:
        os.makedirs(root, exist_ok=True)
    return Workspace(tempfile.mkdtemp(prefix="biochef-run-", dir=root or None))
