"""The Node screen (section 8). Empty until its part arrives."""

from fastapi import APIRouter, Request

from ui.render import page

router = APIRouter()


@router.get("/ui/node")
def node(request: Request):
    return page(request, "node.html", "Node")
