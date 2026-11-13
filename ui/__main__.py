"""`python -m ui`: start the page on the configured address.

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
    uvicorn.run(create_app(configured), host=configured.host, port=configured.port,
                proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
