"""The Inbox screen (section 8). Empty until its part arrives."""

from fastapi import APIRouter, Request

from ui.render import page

router = APIRouter()


@router.get("/ui/inbox")
def inbox(request: Request):
    return page(request, "inbox.html", "Inbox")
