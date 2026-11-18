"""Undo, from whichever page shows the decision (sections 8.1 and 13)."""

from fastapi import APIRouter, Request

from node import egress, review
from store import repo
from store.states import SubjectType
from ui.actions import act

router = APIRouter()


@router.post("/ui/_/decisions/{decision_id}/undo")
def undo(request: Request, decision_id: int):
    def work(session):
        decision = repo.get_decision(session, decision_id)
        if decision.subject_type == SubjectType.EGRESS.value:
            egress.undo_review(session, decision, user=request.state.user)
        else:
            review.undo(session, decision_id, user=request.state.user)
    return act(request, work)
