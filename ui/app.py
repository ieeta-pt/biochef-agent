"""The UI's ASGI app: `uvicorn ui.app:app`, or `python -m ui`.

docs, redoc and openapi are off. FastAPI's documentation pages load from a CDN,
which breaks the rule that nothing leaves the TRE, and this app has no API to
document.

The middleware added last runs first, so the order of a request is security
headers, identity, CSRF, then the route.
"""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ui import settings as ui_settings
from ui.csrf import CsrfMiddleware
from ui.identity import IdentityMiddleware
from ui.render import HERE
from ui.routes import datasets, inbox, node, requests, studies
from ui.security import SecurityHeadersMiddleware


def create_app(settings):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    app.state.settings = settings

    for screen in (inbox, studies, requests, datasets, node):
        app.include_router(screen.router)
    app.mount("/ui/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/ui/inbox", status_code=303)

    app.add_middleware(CsrfMiddleware, settings=settings)
    app.add_middleware(IdentityMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def __getattr__(name):
    # Built on demand rather than at import, so that importing this module for
    # create_app does not need the whole configuration, while `ui.app:app` still
    # refuses to start without it.
    if name == "app":
        return create_app(ui_settings.load())
    raise AttributeError(name)
