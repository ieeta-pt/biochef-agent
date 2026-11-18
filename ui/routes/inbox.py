"""The Inbox: what waits for a decision on this node (section 8.1)."""

from typing import Optional

from fastapi import APIRouter, Form, Request

from node import review
from store import db, repo
from ui.actions import act, bind_of, clean_comment
from ui.render import fragment, page
from ui.views import first_study, inbox_items, study_detail

router = APIRouter()


def _shown(request, version_id=None):
    """The list, and the detail of the chosen study version or of the first one."""
    state = request.app.state
    with db.reading(bind_of(request)) as session:
        items = inbox_items(session)
        chosen = version_id or first_study(items)
        detail = None
        if chosen is not None:
            version = repo.get_study_version(session, chosen)
            detail = study_detail(session, version, state.settings, state.policy)
            items = inbox_items(session, selected=chosen)
    return items, detail


@router.get("/ui/inbox")
def inbox(request: Request):
    items, detail = _shown(request)
    return page(request, "inbox.html", "Inbox", inbox_items=items, detail=detail)


@router.get("/ui/inbox/study/{version_id}")
def inbox_study(request: Request, version_id: int):
    items, detail = _shown(request, version_id)
    return page(request, "inbox.html", "Inbox", inbox_items=items, detail=detail)


@router.get("/ui/_/inbox/study/{version_id}")
def inbox_study_fragment(request: Request, version_id: int):
    items, detail = _shown(request, version_id)
    return fragment(request, "partials/inbox_pick.html", inbox_items=items,
                    detail=detail)


@router.post("/ui/_/inbox/study/{version_id}/approve")
def approve(request: Request, version_id: int,
            dac_confirmed: Optional[str] = Form(None),
            comment: Optional[str] = Form(None)):
    state = request.app.state

    def work(session):
        review.approve_study(
            session, state.keys, node=state.settings.node_name,
            version_id=version_id, user=request.state.user,
            dac_confirmed=dac_confirmed == "on", comment=clean_comment(comment))
    return act(request, work, then=f"/ui/inbox/study/{version_id}")


@router.post("/ui/_/inbox/study/{version_id}/refuse")
def refuse(request: Request, version_id: int,
           comment: Optional[str] = Form(None)):
    state = request.app.state

    def work(session):
        review.refuse_study(
            session, state.keys, node=state.settings.node_name,
            version_id=version_id, user=request.state.user,
            comment=clean_comment(comment))
    return act(request, work, then=f"/ui/inbox/study/{version_id}")
