"""The UI's ASGI app, started by `python -m ui` and by nothing else.

There is no module-level `app` for `uvicorn ui.app:app`. uvicorn started that way
believes X-Forwarded-For from 127.0.0.1 unless told not to, and then any local
process can pose as a trusted proxy and name any user (ui/identity.py).

docs, redoc and openapi are off. FastAPI's documentation pages load from a CDN,
which breaks the rule that nothing leaves the TRE, and this app has no API to
document.

The middleware added last runs first, so the order of a request is security
headers, identity, CSRF, the body cap, then the route. The cap is the one the
CSRF check applies to a form, for every request: nothing here takes an upload.
"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from bodylimit import BodySizeLimitMiddleware
from node import keys as node_keys
from node.policy import FakePolicyEngine
from store import db, migrate
from store.errors import NotFound
from ui import settings as ui_settings
from ui.csrf import MAX_FORM_BYTES, CsrfMiddleware
from ui.identity import IdentityMiddleware
from ui.render import HERE
from ui.routes import datasets, decisions, inbox, node, requests, studies, workflows
from ui.security import SecurityHeadersMiddleware


def create_app(settings, *, bind, keys, policy=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    app.state.settings = settings
    app.state.bind = bind
    app.state.keys = keys
    app.state.policy = policy

    for screen in (inbox, studies, workflows, decisions, requests, datasets, node):
        app.include_router(screen.router)
    app.mount("/ui/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/ui/inbox", status_code=303)

    @app.exception_handler(NotFound)
    def not_found(_request, _failure):
        return PlainTextResponse("No such record on this node.\n", status_code=404)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_FORM_BYTES)
    app.add_middleware(CsrfMiddleware, settings=settings)
    app.add_middleware(IdentityMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def configured_app():
    """The app of this process, or a refusal to start.

    Everything a decision needs is checked here rather than at the first click:
    the signing key, and a database at the current schema.
    """
    settings = ui_settings.load()
    bind = db.engine()
    if not migrate.is_current(bind):
        raise ui_settings.SettingsError("the database schema is not current; run: "
                                        "python -m store migrate")
    # Development has no Policy Engine to ask, and the page labels this one.
    policy = FakePolicyEngine() if settings.identity == ui_settings.DEV else None
    return create_app(settings, bind=bind, keys=node_keys.load(), policy=policy)

