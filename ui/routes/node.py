"""Node: how this node is set up, and its audit trail (section 8.7).

Read from the file by this process, and never through the API: an audit trail
reachable over the service it audits is one an attacker reads to find out what
was noticed.
"""

import os

import audit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from store import db, repo
from ui.actions import bind_of
from ui.render import page
from ui.views import node_page

router = APIRouter()


@router.get("/ui/node")
def screen(request: Request):
    state = request.app.state
    events = audit.read()
    with db.reading(bind_of(request)) as session:
        data = node_page(session, state.settings, state.keys, events, repo.utcnow())
    return page(request, "node.html", "Node", data=data)


@router.get("/ui/node/audit.jsonl")
def export(request: Request):
    path = audit.path()
    if path is None or not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/x-ndjson",
                        filename="audit.jsonl")
