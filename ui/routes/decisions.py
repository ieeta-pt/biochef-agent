"""Undo, from whichever page shows the decision (sections 8.1 and 13)."""

from fastapi import APIRouter, Request

from node import review
from ui.actions import act

router = APIRouter()


@router.post("/ui/_/decisions/{decision_id}/undo")
def undo(request: Request, decision_id: int):
    def work(session):
        review.undo(session, decision_id, user=request.state.user)
    return act(request, work)
