"""The Requests screen (section 8). Empty until its part arrives."""

from fastapi import APIRouter, Request

from ui.render import page

router = APIRouter()


@router.get("/ui/requests")
def requests(request: Request):
    return page(request, "requests.html", "Requests")
