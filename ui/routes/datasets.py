"""The Datasets screen (section 8). Empty until its part arrives."""

from fastapi import APIRouter, Request

from ui.render import page

router = APIRouter()


@router.get("/ui/datasets")
def datasets(request: Request):
    return page(request, "datasets.html", "Datasets")
