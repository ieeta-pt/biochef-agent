"""The Studies screen (section 8). Empty until its part arrives."""

from fastapi import APIRouter, Request

from ui.render import page

router = APIRouter()


@router.get("/ui/studies")
def studies(request: Request):
    return page(request, "studies.html", "Studies")
