"""Templates, with autoescape on for all of them.

Set here rather than left to the default, which depends on the file extension,
because these pages show text written by study members and by running tools.
"""

from pathlib import Path

import jinja2
from starlette.templating import Jinja2Templates

from store import db, repo
from ui import format as fmt
from ui import nav, views

HERE = Path(__file__).resolve().parent

_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(HERE / "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)
_environment.filters["short_digest"] = fmt.short_digest
_templates = Jinja2Templates(env=_environment)


def fragment(request, template, *, status_code=200, **context):
    """Part of a page, for htmx."""
    context.setdefault("settings", request.app.state.settings)
    return _templates.TemplateResponse(request, template, context,
                                       status_code=status_code)


def page(request, template, active, **context):
    """A full page, with what the sidebar needs."""
    settings = request.app.state.settings
    with db.reading(request.app.state.bind) as session:
        side = views.sidebar(session, repo.utcnow())
    return _templates.TemplateResponse(request, template, {
        "items": nav.items(side["counts"]),
        "polled": side["polled"],
        "active": active,
        "settings": settings,
        "user": request.state.user,
        "csrf_token": request.state.csrf_token,
        **context,
    })
