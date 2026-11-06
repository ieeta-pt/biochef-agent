"""Shared by the UI tests: a configured app and clients at chosen addresses."""

import ipaddress
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from ui.app import create_app
from ui.settings import DEV, HEADER, Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
UI = REPO_ROOT / "ui"

PAGES = ("/ui/inbox", "/ui/studies", "/ui/requests", "/ui/datasets", "/ui/node")

PROXY = "10.0.0.5"
USER_HEADER = "X-Forwarded-User"
LOOPBACK = "127.0.0.1"


def dev_settings(**overrides):
    fields = {"identity": DEV, "node_name": "Example TRE (test)",
              "dev_user": "dev.user"}
    return Settings(**{**fields, **overrides})


def header_settings(**overrides):
    fields = {"identity": HEADER, "node_name": "Example TRE (test)",
              "identity_header": USER_HEADER,
              "trusted_proxies": (ipaddress.ip_network("10.0.0.0/24"),)}
    return Settings(**{**fields, **overrides})


def client_for(settings, address=LOOPBACK, **options):
    # https, because a Secure cookie does not come back over http.
    return TestClient(create_app(settings), client=(address, 50000),
                      base_url="https://ui.tre.test", follow_redirects=False,
                      **options)


def dev_client(**settings):
    return client_for(dev_settings(**settings))


def proxy_client(**options):
    client = client_for(header_settings(), PROXY, **options)
    client.headers[USER_HEADER] = "j.silva"
    return client


def with_routes(settings, address=LOOPBACK, **options):
    """An app with one state changing route and one that crashes.

    The shell has neither yet, and both are what the middleware is for.
    """
    app = create_app(settings)

    @app.post("/ui/_/echo")
    async def echo(request: Request):
        return PlainTextResponse((await request.body()).decode())

    @app.get("/ui/_/crash")
    def crash():
        raise RuntimeError("secret detail")

    return TestClient(app, client=(address, 50000), follow_redirects=False,
                      base_url="https://ui.tre.test", **options)
