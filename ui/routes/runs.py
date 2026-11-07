"""One run: its steps, its egress, its logs and its manifest (section 8.5)."""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from node import decisions, egress
from store import db, repo
from store.states import Action, SubjectType
from ui.actions import act, bind_of
from ui.render import fragment, page
from ui.views import run_page

router = APIRouter()

TABS = ("egress", "logs", "manifest")


def _detail(request, run_id, tab="egress", log="run"):
    if tab not in TABS:
        raise HTTPException(status_code=404)
    with db.reading(bind_of(request)) as session:
        run = repo.get_run(session, run_id)
        detail = run_page(session, run, tab, request.app.state.settings, log)
        detail["manifest"] = _manifest_text(run.manifest_path)
    return detail


def _manifest_text(path):
    """run.json, while the workspace is still kept. A link cannot stand in for it."""
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read().decode("utf-8", errors="replace")


@router.get("/ui/runs/{run_id}")
def run(request: Request, run_id: str, tab: str = "egress", log: str = "run"):
    return page(request, "run.html", "Requests", detail=_detail(request, run_id, tab, log))


@router.get("/ui/_/runs/{run_id}/tab/{tab}")
def run_tab(request: Request, run_id: str, tab: str, log: str = "run"):
    return fragment(request, "partials/run_tabs.html",
                    detail=_detail(request, run_id, tab, log))


@router.get("/ui/_/runs/{run_id}/log")
def run_log(request: Request, run_id: str, log: str = "run"):
    return fragment(request, "partials/run_log.html",
                    detail=_detail(request, run_id, "logs", log))


@router.get("/ui/runs/{run_id}/download/log")
def download_log(request: Request, run_id: str, log: str = "run"):
    detail = _detail(request, run_id, "logs", log)
    return PlainTextResponse(detail["log_text"], headers={
        "Content-Disposition": f'attachment; filename="{run_id}.log"'})


@router.get("/ui/runs/{run_id}/download/run.json")
def download_manifest(request: Request, run_id: str):
    detail = _detail(request, run_id, "manifest")
    if detail["manifest"] is None:
        raise HTTPException(status_code=404)
    return Response(detail["manifest"], media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{run_id}-run.json"'})


def _review(request, run_id, decide):
    state = request.app.state

    def work(session):
        if state.settings.egress_mode != egress.MANUAL:
            raise egress.Conflict("This node reviews egress in the TRE's airlock.")
        decide(session, state.keys, node=state.settings.node_name, run_id=run_id,
               user=request.state.user)
    return act(request, work, then=f"/ui/runs/{run_id}")


@router.post("/ui/_/runs/{run_id}/release")
def release(request: Request, run_id: str):
    return _review(request, run_id, egress.release)


@router.post("/ui/_/runs/{run_id}/withhold")
def withhold(request: Request, run_id: str):
    def decide(session, keys, *, node, run_id, user):
        return egress.withhold(session, keys, node=node, run_id=run_id, user=user,
                               keep_seconds=request.app.state.keep_seconds)
    return _review(request, run_id, decide)


@router.post("/ui/_/runs/{run_id}/cancel")
def cancel(request: Request, run_id: str):
    state = request.app.state

    def work(session):
        repo.request_cancel(session, run_id)
        decisions.decide(session, state.keys, node=state.settings.node_name,
                         subject_type=SubjectType.RUN, subject_id=run_id,
                         action=Action.CANCEL, user=request.state.user,
                         at=repo.utcnow())
    return act(request, work, then=f"/ui/runs/{run_id}")
