"""Requests: the last 24 hours, and how far each one got (section 8.4)."""

from fastapi import APIRouter, HTTPException, Request

from store import db, repo
from ui.actions import bind_of
from ui.render import fragment, page
from ui.views import FILTERS, requests_page

router = APIRouter()


def _data(request, shown, selected):
    if shown not in FILTERS:
        raise HTTPException(status_code=404)
    with db.reading(bind_of(request)) as session:
        return requests_page(session, repo.utcnow(), shown, selected)


@router.get("/ui/requests")
def screen(request: Request, filter: str = "all", selected: str = None):
    return page(request, "requests.html", "Requests",
                data=_data(request, filter, selected))


@router.get("/ui/_/requests")
def part(request: Request, filter: str = "all", selected: str = None):
    return fragment(request, "partials/requests_body.html",
                    data=_data(request, filter, selected))
