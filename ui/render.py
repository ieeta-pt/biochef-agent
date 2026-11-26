"""Templates, with autoescape on for all of them.

Set here rather than left to the default, which depends on the file extension,
because these pages show text written by study members and by running tools.
"""

from pathlib import Path

import jinja2
from starlette.templating import Jinja2Templates

from ui import nav

HERE = Path(__file__).resolve().parent

_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(HERE / "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)
_templates = Jinja2Templates(env=_environment)


def page(request, template, active):
    """A full page, with what the sidebar needs."""
    settings = request.app.state.settings
    return _templates.TemplateResponse(request, template, {
        "items": nav.ITEMS,
        "active": active,
        "settings": settings,
        "user": request.state.user,
        "csrf_token": request.state.csrf_token,
    })
