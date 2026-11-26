"""`python -m ui`: start the page on the configured address or unix socket.

The address comes from the same settings the app checks, .env included, so the
two cannot disagree. proxy_headers stays off because the identity check depends
on the address of the socket (ui/identity.py).
"""

import sys

import uvicorn

from ui import settings
from ui.app import create_app


def main():
    try:
        configured = settings.load()
    except settings.SettingsError as refused:
        print(refused, file=sys.stderr)
        return 1
    where = ({"uds": configured.socket} if configured.socket is not None
             else {"host": configured.host, "port": configured.port})
    uvicorn.run(create_app(configured), **where, proxy_headers=False,
                server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
