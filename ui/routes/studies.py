"""Studies: what this node approved, and everything that became of it (8.2)."""

from fastapi import APIRouter, HTTPException, Request

from node import review
from store import db, repo
from ui.actions import act, bind_of
from ui.render import fragment, page
from ui.views import (first_card, request_rows, study_cards, study_page,
                      workflow_rows)

router = APIRouter()

TABS = ("overview", "workflows", "requests")


def _tab(name):
    if name not in TABS:
        raise HTTPException(status_code=404)
    return name


def _detail(session, request, study_id, tab):
    detail = study_page(session, study_id, request.app.state.settings, tab)
    if detail is None:
        raise HTTPException(status_code=404)
    detail["workflows"] = workflow_rows(session, detail["version_id"])
    detail["requests"] = request_rows(session, study_id)
    return detail


def _page(request, study_id, tab):
    with db.reading(bind_of(request)) as session:
        cards = study_cards(session, selected=study_id)
        chosen = study_id or first_card(cards)
        if study_id is None and chosen is not None:
            cards = study_cards(session, selected=chosen)
        detail = _detail(session, request, chosen, tab) if chosen else None
    return page(request, "studies.html", "Studies", cards=cards, detail=detail)


@router.get("/ui/studies")
def studies(request: Request):
    return _page(request, None, "overview")


@router.get("/ui/studies/{study_id}")
def study(request: Request, study_id: str, tab: str = "overview"):
    return _page(request, study_id, _tab(tab))


@router.get("/ui/_/studies/{study_id}/tab/{tab}")
def study_tab(request: Request, study_id: str, tab: str):
    with db.reading(bind_of(request)) as session:
        detail = _detail(session, request, study_id, _tab(tab))
    return fragment(request, "partials/study_tabs.html", detail=detail)


@router.get("/ui/_/studies/{study_id}/requests")
def older_requests(request: Request, study_id: str, offset: int = 0):
    with db.reading(bind_of(request)) as session:
        rows = request_rows(session, study_id, offset=max(0, offset))
    return fragment(request, "partials/request_rows.html", requests=rows)


@router.get("/ui/_/studies/version/{version_id}/withdraw")
def withdraw_confirm(request: Request, version_id: int):
    with db.reading(bind_of(request)) as session:
        version = repo.get_study_version(session, version_id)
    return fragment(request, "partials/withdraw_confirm.html",
                    version_id=version_id, version=version.version)


@router.post("/ui/_/studies/version/{version_id}/withdraw")
def withdraw(request: Request, version_id: int):
    state = request.app.state
    with db.reading(bind_of(request)) as session:
        study_id = repo.get_study_version(session, version_id).study_id

    def work(session):
        review.withdraw_study(session, state.keys, node=state.settings.node_name,
                              version_id=version_id, user=request.state.user)
    return act(request, work, then=f"/ui/studies/{study_id}")
