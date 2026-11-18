"""One workflow version: its steps, what changed, and the decision (8.3)."""

from fastapi import APIRouter, Request

from node import review
from store import db, repo
from ui import source
from ui.actions import act, bind_of
from ui.render import fragment, page
from ui.views import version_title, workflow_page, workflow_title

router = APIRouter()


def _show(request, workflow_id, step=None):
    with db.reading(bind_of(request)) as session:
        workflow = repo.get_workflow_version(session, workflow_id)
        return workflow_page(session, workflow, step)


@router.get("/ui/workflows/{workflow_id}")
def workflow(request: Request, workflow_id: int):
    return page(request, "workflow.html", "Studies",
                detail=_show(request, workflow_id))


@router.get("/ui/_/workflows/{workflow_id}/step/{index}")
def step(request: Request, workflow_id: int, index: int):
    return fragment(request, "partials/workflow_steps.html",
                    detail=_show(request, workflow_id, index))


@router.get("/ui/workflows/{workflow_id}/source")
def workflow_source(request: Request, workflow_id: int):
    with db.reading(bind_of(request)) as session:
        row = repo.get_workflow_version(session, workflow_id)
        version = repo.get_study_version(session, row.study_version_id)
        title = workflow_title(row)
        study_title = version_title(session, version)
        raw = row.raw
    text, problem = source.snakefile(raw)
    return page(request, "workflow_source.html", "Studies", workflow_id=workflow_id,
                title=title, study_title=study_title,
                recipe=source.workflow_json(raw), snakefile=text, problem=problem)


def _decide(request, workflow_id, decide, then=None):
    state = request.app.state

    def work(session):
        decide(session, state.keys, node=state.settings.node_name,
               workflow_id=workflow_id, user=request.state.user)
    return act(request, work, then=then or f"/ui/workflows/{workflow_id}")


@router.post("/ui/_/workflows/{workflow_id}/approve")
def approve(request: Request, workflow_id: int):
    return _decide(request, workflow_id, review.approve_workflow)


@router.post("/ui/_/workflows/{workflow_id}/refuse")
def refuse(request: Request, workflow_id: int):
    return _decide(request, workflow_id, review.refuse_workflow)


@router.post("/ui/_/workflows/{workflow_id}/withdraw")
def withdraw(request: Request, workflow_id: int):
    with db.reading(bind_of(request)) as session:
        study_id = repo.get_workflow_version(session, workflow_id).study_id
    return _decide(request, workflow_id, review.withdraw_workflow,
                   then=f"/ui/studies/{study_id}?tab=workflows")
