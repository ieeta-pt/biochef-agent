"""What the TES API is configured with, and the refusals to start without it.

Everything that decides what a task may touch has no default: the images, the
directories and the credentials. A listener that started with a guess would be
the thing it exists to prevent.
"""

import hashlib
import ipaddress
import os
import re
import stat
from typing import NamedTuple, Tuple

from store import db

IMAGE_SHAPE = re.compile(r"\A(?!.*//)[A-Za-z0-9][A-Za-z0-9._:/-]{0,250}@sha256:[0-9a-f]{64}\Z")
NAME_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TOKEN_LINE = re.compile(r"\A(\S+)\s+sha256:([0-9a-f]{64})\Z")
LOOPBACK = ("localhost", "127.0.0.1", "::1")

MAX_KEPT_TASKS = 1000
"""Finished tasks the listener remembers. The oldest go first."""

MAX_BODY_BYTES = 1024 * 1024
"""A task is a few lines of JSON, and inline input content is limited by itself."""


class ConfigError(ValueError):
    """A setting the listener will not start with."""


class Config(NamedTuple):
    host: str
    port: int
    tokens: Tuple[Tuple[str, str], ...]
    images: Tuple[str, ...]
    read_dirs: Tuple[str, ...]
    write_dirs: Tuple[str, ...]
    denied: Tuple[str, ...]
    max_concurrent: int
    max_queued: int
    max_bytes: int
    timeout_s: int
    tls_cert: str
    tls_key: str
    run_root: str
    db_url: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_tokens(path: str):
    """name -> SHA-256 of the token, from a file only this user can read.

    The tokens themselves are never stored, so a copy of the file is not a way in.
    """
    if not path or not os.path.isabs(path):
        raise ConfigError("BIOCHEF_TESAPI_TOKENS must be the absolute path of a file "
                          "of `name sha256:<hex>` lines, and has no default: the "
                          "listener does not run without authentication")
    try:
        info = os.lstat(path)
    except OSError as error:
        raise ConfigError(f"BIOCHEF_TESAPI_TOKENS={path!r}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"BIOCHEF_TESAPI_TOKENS={path!r} is not a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError(f"BIOCHEF_TESAPI_TOKENS={path!r} must belong to this user "
                          f"and be readable by no one else (mode 0600)")
    entries, names = [], set()
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream.read().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            found = TOKEN_LINE.match(line.strip())
            if not found or not NAME_SHAPE.match(found.group(1)):
                raise ConfigError(f"{path}:{number}: expected `name sha256:<64 hex>`")
            if found.group(1) in names:
                raise ConfigError(f"{path}:{number}: {found.group(1)} appears twice")
            names.add(found.group(1))
            entries.append((found.group(1), found.group(2)))
    if not entries:
        raise ConfigError(f"{path} lists no token, so nobody could call the listener")
    return tuple(entries)


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def overlaps(first: str, second: str) -> bool:
    return within(first, second) or within(second, first)


def _integer(name, raw, minimum, maximum=None):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}={raw!r} must be a whole number") from None
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{name}={raw!r} is out of range")
    return value


def _denied(values):
    """Places no task may read from or write to, whatever the directories say."""
    found = []
    for name in ("BIOCHEF_LOCAL_ROOT", "BIOCHEF_RUN_ROOT", "BIOCHEF_AUDIT_LOG",
                 "BIOCHEF_NODE_SIGNING_KEY", "BIOCHEF_NODE_ENCRYPTION_KEY",
                 "BIOCHEF_TES_STAGING", "BIOCHEF_APPTAINER_CACHE",
                 "BIOCHEF_TESAPI_TOKENS", "BIOCHEF_TESAPI_TLS_KEY",
                 "BIOCHEF_TES_TOKEN_FILE", "BIOCHEF_TES_PASSWORD_FILE"):
        raw = (values.get(name) or "").strip()
        if raw:
            found.append((name, os.path.realpath(raw)))
    url = (values.get("BIOCHEF_DB_URL") or "").strip() or db.DEFAULT_URL
    if url.startswith("sqlite:"):
        file = url.split(":", 1)[1].lstrip("/")
        if file and file != ":memory:":
            found.append(("BIOCHEF_DB_URL", os.path.realpath("/" + file)))
    return tuple(found)


def _directories(name, raw, denied):
    """The directories one list names, each absolute, normal, real and not the node's."""
    found = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not os.path.isabs(part):
            raise ConfigError(f"{name}: {part!r} is not an absolute path")
        real = os.path.realpath(part)
        if real != os.path.normpath(part):
            raise ConfigError(f"{name}: {part!r} is named through a symlink or is not "
                              f"normal; give {real!r}")
        if not os.path.isdir(real):
            raise ConfigError(f"{name}: {part!r} is not a directory")
        if real == "/":
            raise ConfigError(f"{name}: / would allow everything")
        for other_name, other in denied:
            if overlaps(real, other):
                raise ConfigError(
                    f"{name}: {real} overlaps {other} ({other_name}), which "
                    f"holds the node's own data. No task may read or write there, so "
                    f"no directory that contains it or lies inside it can be allowed")
        found.append(real)
    if not found:
        raise ConfigError(f"{name} lists no directory, and has no default: a task "
                          f"reads and writes only where it is told")
    return tuple(found)


def settings_from_environment():
    """Every setting, read where the settings test can see it."""
    return {
        "BIOCHEF_TESAPI_HOST": os.getenv("BIOCHEF_TESAPI_HOST", "127.0.0.1"),
        "BIOCHEF_TESAPI_PORT": os.getenv("BIOCHEF_TESAPI_PORT", "8082"),
        "BIOCHEF_TESAPI_TOKENS": os.getenv("BIOCHEF_TESAPI_TOKENS", ""),
        "BIOCHEF_TESAPI_IMAGES": os.getenv("BIOCHEF_TESAPI_IMAGES", ""),
        "BIOCHEF_TESAPI_READ_DIRS": os.getenv("BIOCHEF_TESAPI_READ_DIRS", ""),
        "BIOCHEF_TESAPI_WRITE_DIRS": os.getenv("BIOCHEF_TESAPI_WRITE_DIRS", ""),
        "BIOCHEF_TESAPI_DIRS": os.getenv("BIOCHEF_TESAPI_DIRS", ""),
        "BIOCHEF_TESAPI_MAX_CONCURRENT": os.getenv("BIOCHEF_TESAPI_MAX_CONCURRENT", "1"),
        "BIOCHEF_TESAPI_MAX_QUEUED": os.getenv("BIOCHEF_TESAPI_MAX_QUEUED", "16"),
        "BIOCHEF_TESAPI_MAX_BYTES": os.getenv("BIOCHEF_TESAPI_MAX_BYTES",
                                              str(1024 ** 3)),
        "BIOCHEF_TESAPI_TIMEOUT": os.getenv("BIOCHEF_TESAPI_TIMEOUT", "900"),
        "BIOCHEF_TESAPI_TLS_CERT": os.getenv("BIOCHEF_TESAPI_TLS_CERT", ""),
        "BIOCHEF_TESAPI_TLS_KEY": os.getenv("BIOCHEF_TESAPI_TLS_KEY", ""),
        "BIOCHEF_LOCAL_ROOT": os.getenv("BIOCHEF_LOCAL_ROOT", ""),
        "BIOCHEF_RUN_ROOT": os.getenv("BIOCHEF_RUN_ROOT", ""),
        "BIOCHEF_AUDIT_LOG": os.getenv("BIOCHEF_AUDIT_LOG", ""),
        "BIOCHEF_DB_URL": os.getenv("BIOCHEF_DB_URL", ""),
        "BIOCHEF_NODE_SIGNING_KEY": os.getenv("BIOCHEF_NODE_SIGNING_KEY", ""),
        "BIOCHEF_NODE_ENCRYPTION_KEY": os.getenv("BIOCHEF_NODE_ENCRYPTION_KEY", ""),
        "BIOCHEF_TES_STAGING": os.getenv("BIOCHEF_TES_STAGING", ""),
        "BIOCHEF_APPTAINER_CACHE": os.getenv("BIOCHEF_APPTAINER_CACHE", ""),
        "BIOCHEF_TES_TOKEN_FILE": os.getenv("BIOCHEF_TES_TOKEN_FILE", ""),
        "BIOCHEF_TES_PASSWORD_FILE": os.getenv("BIOCHEF_TES_PASSWORD_FILE", ""),
    }


def load(values=None) -> Config:
    values = settings_from_environment() if values is None else values

    def get(name, default=""):
        value = values.get(name)
        return default if value is None else str(value).strip()

    if not get("BIOCHEF_AUDIT_LOG"):
        raise ConfigError("BIOCHEF_AUDIT_LOG must be set: the listener starts "
                          "nothing it cannot put on the record")
    host = get("BIOCHEF_TESAPI_HOST", "127.0.0.1")
    port = _integer("BIOCHEF_TESAPI_PORT", get("BIOCHEF_TESAPI_PORT", "8082"), 1, 65535)
    cert, key = get("BIOCHEF_TESAPI_TLS_CERT"), get("BIOCHEF_TESAPI_TLS_KEY")
    local = host in LOOPBACK
    if not local:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
    if not local and not (cert and key):
        raise ConfigError(
            f"BIOCHEF_TESAPI_HOST={host!r} is not this host, and the listener "
            f"carries tokens and tasks: set BIOCHEF_TESAPI_TLS_CERT and "
            f"BIOCHEF_TESAPI_TLS_KEY, or listen on 127.0.0.1")
    for name, path in (("BIOCHEF_TESAPI_TLS_CERT", cert), ("BIOCHEF_TESAPI_TLS_KEY", key)):
        if bool(cert) != bool(key):
            raise ConfigError("BIOCHEF_TESAPI_TLS_CERT and BIOCHEF_TESAPI_TLS_KEY "
                              "go together")
        if path and not (os.path.isabs(path) and os.path.isfile(path)):
            raise ConfigError(f"{name}={path!r} must be the absolute path of a file")
    tokens = read_tokens(get("BIOCHEF_TESAPI_TOKENS"))

    images = tuple(sorted({part.strip() for part in
                           get("BIOCHEF_TESAPI_IMAGES").split(",") if part.strip()}))
    if not images:
        raise ConfigError("BIOCHEF_TESAPI_IMAGES lists no image, and has no default: "
                          "a task runs only an image named here")
    for image in images:
        if not IMAGE_SHAPE.match(image):
            raise ConfigError(f"BIOCHEF_TESAPI_IMAGES: {image!r} is not an image by "
                              f"digest (repository@sha256:<64 hex>)")

    denied = _denied(values)
    if get("BIOCHEF_TESAPI_DIRS"):
        raise ConfigError("BIOCHEF_TESAPI_DIRS is replaced by BIOCHEF_TESAPI_READ_DIRS and "
                          "BIOCHEF_TESAPI_WRITE_DIRS: a task reads from one set of "
                          "directories and writes to another, and they may not share "
                          "any. Unset it and list the two")
    read_dirs = _directories("BIOCHEF_TESAPI_READ_DIRS", get("BIOCHEF_TESAPI_READ_DIRS"), denied)
    write_dirs = _directories("BIOCHEF_TESAPI_WRITE_DIRS", get("BIOCHEF_TESAPI_WRITE_DIRS"), denied)
    for reading in read_dirs:
        for writing in write_dirs:
            if overlaps(reading, writing):
                raise ConfigError(
                    f"BIOCHEF_TESAPI_READ_DIRS and BIOCHEF_TESAPI_WRITE_DIRS share a "
                    f"directory: {reading} and {writing} are the same place or one is "
                    f"inside the other. What a task may read and what it may write "
                    f"are kept apart, so that nothing it wrote can be read back as an "
                    f"input, and nothing it read can be overwritten")
    return Config(
        host, port, tokens, images, read_dirs, write_dirs, tuple(path for _, path in denied),
        _integer("BIOCHEF_TESAPI_MAX_CONCURRENT", get("BIOCHEF_TESAPI_MAX_CONCURRENT", "1"), 1, 256),
        _integer("BIOCHEF_TESAPI_MAX_QUEUED", get("BIOCHEF_TESAPI_MAX_QUEUED", "16"), 0, 100000),
        _integer("BIOCHEF_TESAPI_MAX_BYTES", get("BIOCHEF_TESAPI_MAX_BYTES", str(1024 ** 3)), 1),
        _integer("BIOCHEF_TESAPI_TIMEOUT", get("BIOCHEF_TESAPI_TIMEOUT", "900"), 1),
        cert, key, get("BIOCHEF_RUN_ROOT"),
        get("BIOCHEF_DB_URL") or db.DEFAULT_URL)
