"""`python -m ui`: start the page on the configured address.

The address comes from the same settings the app checks, .env included, so the
two cannot disagree. proxy_headers stays off because the identity check depends
on the address of the socket (ui/identity.py).
"""

import sys

import uvicorn

from node.keys import NodeKeyError
from store.errors import StoreError
from ui import app, settings


def main():
    try:
        configured = app.configured_app()
    except (settings.SettingsError, NodeKeyError, StoreError) as refused:
        print(refused, file=sys.stderr)
        return 1
    current = configured.state.settings
    uvicorn.run(configured, host=current.host, port=current.port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
