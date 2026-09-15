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
