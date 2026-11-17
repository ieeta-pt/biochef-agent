"""The node's two keys.

Ed25519 signs every decision and every result, so whoever holds it can approve
in this node's name. X25519 decrypts request payloads. Neither is something to
find out about later, so a key that is missing, unreadable or exposed stops the
service at start rather than at the first signature.

Readable by group or others counts as exposed, the same rule ssh applies, and a
key inside this repository counts too: one `git add .` away from being public.

`python -m node.keys init --out <dir>` makes both and prints their public
fingerprints, for registering the node with the federation. It never prints a
private key and never overwrites one, because a node that replaces its key
silently stops matching what the federation knows about it.
"""

import argparse
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

SIGNING_FILE = "node-signing.pem"
ENCRYPTION_FILE = "node-encryption.pem"

REPO_ROOT = Path(__file__).resolve().parent.parent


class NodeKeyError(Exception):
    """A key could not be used. Never carries any of the key's content."""


def _public_bytes(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(kind, private_key):
    """`ed25519:<sha256 of the raw public key>`, for registration."""
    return f"{kind}:{hashlib.sha256(_public_bytes(private_key)).hexdigest()}"


def short_fingerprint(full):
    """The form the page shows: first and last 4 hex characters."""
    kind, _, digest = full.partition(":")
    return f"{kind}:{digest[:4]}…{digest[-4:]}"


@dataclass(frozen=True)
class NodeKeys:
    signing: Ed25519PrivateKey
    encryption: X25519PrivateKey

    @property
    def signing_fingerprint(self):
        return fingerprint("ed25519", self.signing)

    @property
    def encryption_fingerprint(self):
        return fingerprint("x25519", self.encryption)

    def __repr__(self):
        return (f"NodeKeys({short_fingerprint(self.signing_fingerprint)}, "
                f"{short_fingerprint(self.encryption_fingerprint)})")


def generate():
    return NodeKeys(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


def _refuse_inside_the_repo(path, what):
    resolved = Path(os.path.realpath(path))
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise NodeKeyError(f"{what} is inside the repository ({resolved}); "
                           f"keys live outside it")


def _read(path, what):
    # Everything is checked on the open descriptor, so that the file checked is
    # the file read.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as failure:
        raise NodeKeyError(f"{what} at {path} cannot be read: "
                           f"{failure.strerror}") from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise NodeKeyError(f"{what} at {path} is not a file")
        if details.st_mode & 0o077:
            raise NodeKeyError(
                f"{what} at {path} has mode "
                f"{stat.S_IMODE(details.st_mode):04o}; it must be readable by "
                f"its owner only (chmod 600)")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _load(path, setting, expected):
    what = f"the key in {setting}"
    if not path:
        raise NodeKeyError(f"{setting} is not set. Create the keys with: "
                           f"python -m node.keys init --out <dir>")
    _refuse_inside_the_repo(path, what)
    data = _read(path, what)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        # `from None`, and the library's message left out: what a parser says
        # about a key file has no business in a log.
        raise NodeKeyError(f"{what} at {path} is not an unencrypted PEM "
                           f"private key") from None
    if not isinstance(key, expected):
        raise NodeKeyError(f"{what} at {path} is the wrong type of key")
    return key


def load(signing_path=None, encryption_path=None):
    """Both keys, from the settings or the given paths, or a refusal."""
    return NodeKeys(
        _load(signing_path or os.getenv("BIOCHEF_NODE_SIGNING_KEY"),
              "BIOCHEF_NODE_SIGNING_KEY", Ed25519PrivateKey),
        _load(encryption_path or os.getenv("BIOCHEF_NODE_ENCRYPTION_KEY"),
              "BIOCHEF_NODE_ENCRYPTION_KEY", X25519PrivateKey),
    )


def _pem(private_key):
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def _write_new(path, data):
    # O_EXCL so that a key already there is never replaced, and the mode given
    # at creation so that there is no moment when the file is wider than this.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def init(out):
    """Create both keys in `out`. Refuses to touch an existing key."""
    directory = Path(out)
    _refuse_inside_the_repo(directory, "the output directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    signing_path = directory / SIGNING_FILE
    encryption_path = directory / ENCRYPTION_FILE
    for path in (signing_path, encryption_path):
        if os.path.lexists(path):
            raise NodeKeyError(f"{path} already exists; not replacing a key")

    keys = generate()
    _write_new(signing_path, _pem(keys.signing))
    _write_new(encryption_path, _pem(keys.encryption))
    return keys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m node.keys")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", help="create the node's two keys")
    create.add_argument("--out", required=True, help="directory for the key files")
    args = parser.parse_args(argv)

    try:
        keys = init(args.out)
    except (NodeKeyError, OSError) as refused:
        print(refused, file=sys.stderr)
        return 1
    print(keys.signing_fingerprint)
    print(keys.encryption_fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
