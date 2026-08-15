"""The names a run is allowed to use for a file.

A workflow's filenames are not user data. The converter generates every one of
them -- `convert.py` builds `f"{source}-{source_handle}"` -- so a name arriving
on an upload is a claim about which of those it is, and the only sensible
question is whether it is one.

This module answers the narrow half of that: whether a name is a single, plain
path component at all. Checking it against the specific set the workflow
declares is the other half and belongs with the run directory work (#40), where
the declared set is available. The two are independent, and each catches
something the other cannot:

  a name like "samtools" is a perfectly good single component, so the shape rule
  passes it -- but it may collide with a tool binary, which only the declared set
  would catch;

  a name derived from a hostile node id can traverse while still being a name
  the workflow declares, which only the shape rule catches.
"""

import re

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
