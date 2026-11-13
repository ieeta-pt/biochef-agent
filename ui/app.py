"""The UI's ASGI app: `uvicorn ui.app:app`, or `python -m ui`.

docs, redoc and openapi are off. FastAPI's documentation pages load from a CDN,
which breaks the rule that nothing leaves the TRE, and this app has no API to
document.

The middleware added last runs first, so the order of a request is security
headers, identity, CSRF, then the route.
"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from node import datasets
from node import keys as node_keys
from node.policy import FakePolicyEngine
from store import db, migrate
from store.errors import NotFound
from ui import settings as ui_settings
from ui.csrf import CsrfMiddleware
from ui.identity import IdentityMiddleware
from ui.render import HERE
from ui.routes import datasets as datasets_screen
from ui.routes import decisions, inbox, node, requests, studies, workflows
from ui.security import SecurityHeadersMiddleware


def create_app(settings, *, bind, keys, policy=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    app.state.settings = settings
    app.state.bind = bind
    app.state.keys = keys
    app.state.policy = policy
    app.state.binding_conflicts = []

    for screen in (inbox, studies, workflows, decisions, requests, datasets_screen,
                   node):
        app.include_router(screen.router)
    app.mount("/ui/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/ui/inbox", status_code=303)

    @app.exception_handler(NotFound)
    def not_found(_request, _failure):
        return PlainTextResponse("No such record on this node.\n", status_code=404)

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
    keys = node_keys.load()
    app = create_app(settings, bind=bind, keys=keys, policy=policy)
    if settings.bindings_file:
        try:
            entries = datasets.load_file(settings.bindings_file, settings.local_root)
            with db.writing(bind) as session:
                app.state.binding_conflicts = datasets.sync(
                    session, keys, settings.node_name, entries)
        except datasets.BindingError as refused:
            raise ui_settings.SettingsError(str(refused)) from None
    return app


def __getattr__(name):
    # Built on demand rather than at import, so that importing this module for
    # create_app does not need the whole configuration, while `ui.app:app` still
    # refuses to start without it.
    if name == "app":
        return configured_app()
    raise AttributeError(name)
