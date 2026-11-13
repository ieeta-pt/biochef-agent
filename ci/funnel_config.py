"""The configuration of the Funnel the CI job starts, made from Funnel's own default.

Funnel panics on a Worker section that leaves its polling rates unset, so this does
not write a config of its own: it takes the default-config.yaml of the Funnel being
tested and changes the few things an operator would, each of which must be found
there or this fails. What it changes:

  Server.HostName      127.0.0.1, so only the job reaches it
  Worker.WorkDir       where the server keeps each task's directories
  BoltDB.Path          its database
  LocalStorage         the one directory it may read and write: the staging root
  Worker.Container     docker run with no network, as an operator who attests
                       network isolation would set it, and Funnel's own --read-only
  Server.BasicAuth     with a user and a password, if given: the server then wants
  RPCClient.Credential them on every call, and so does its own worker (the commented
                       example in the default config names the fields wrongly)

usage: funnel_config.py DEFAULT_CONFIG OUT WORKDIR DATABASE STAGING [USER PASSWORD_FILE]

The password is read from a file and not taken from the command line, and the
config it ends up in is written readable by its owner alone.
"""

import os
import re
import sys


def make(text, workdir, database, staging, user=None, password=None):
    def sub(pattern, replacement):
        nonlocal text
        text, count = re.subn(pattern, lambda _: replacement, text, count=1, flags=re.M)
        if count != 1:
            raise SystemExit(f"funnel_config: {pattern!r} is not in the default config; "
                             f"Funnel changed it, and this needs to follow")

    sub(r"^  HostName: localhost$", "  HostName: 127.0.0.1")
    sub(r"^  WorkDir: \./funnel-work-dir$", f"  WorkDir: {workdir}")
    sub(r"^  Path: \./funnel-work-dir/funnel\.db$", f"  Path: {database}")
    sub(r"^      run -i --read-only$", "      run -i --read-only --network none")
    sub(r"^LocalStorage:\n  # .*\n  AllowedDirs:\n    - \./$",
        f"LocalStorage:\n  AllowedDirs:\n    - {staging}")
    if user is not None:
        sub(r"^  # BasicAuth:\n(?:  #[^\n]*\n)+?(?=\n)",
            f"  BasicAuth:\n    - User: {user}\n      Password: {password}\n")
        sub(r"^  # User: funnel\n  # Password: abc123$",
            f"  Credential:\n    User: {user}\n    Password: {password}")
    return text


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if len(arguments) not in (5, 7):
        raise SystemExit(__doc__)
    default, out, workdir, database, staging = arguments[:5]
    user = password = None
    if len(arguments) == 7:
        user = arguments[5]
        with open(arguments[6]) as stream:
            password = stream.read().strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", user) or not re.fullmatch(
                r"[A-Za-z0-9._~+/=-]{8,256}", password):
            raise SystemExit("funnel_config: the user or the password has a character "
                             "that would need quoting in YAML")
    with open(default) as stream:
        made = make(stream.read(), workdir, database, staging, user, password)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(made)
