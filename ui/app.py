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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from bodylimit import BodySizeLimitMiddleware
from ui.csrf import MAX_FORM_BYTES, CsrfMiddleware
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

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_FORM_BYTES)
    app.add_middleware(CsrfMiddleware, settings=settings)
    app.add_middleware(IdentityMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
