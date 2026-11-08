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

usage: funnel_config.py DEFAULT_CONFIG OUT WORKDIR DATABASE STAGING
"""

import re
import sys


def make(text, workdir, database, staging):
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
    return text


if __name__ == "__main__":
    default, out, workdir, database, staging = sys.argv[1:6]
    with open(default) as stream:
        made = make(stream.read(), workdir, database, staging)
    with open(out, "w") as stream:
        stream.write(made)
