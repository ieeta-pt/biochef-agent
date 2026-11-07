"""Datasets: which folder holds which dataset on this node (section 8.6)."""

from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request

from node import datasets
from store import db, repo
from ui.actions import act, bind_of
from ui.render import fragment, page
from ui.views import dataset_page

router = APIRouter()


def _require_page_bindings(request):
    if not request.app.state.settings.bindings_from_ui:
        raise HTTPException(status_code=403,
                            detail="Bindings on this node come only from the config file.")


@router.get("/ui/datasets")
def screen(request: Request):
    with db.reading(bind_of(request)) as session:
        data = dataset_page(session)
    return page(request, "datasets.html", "Datasets", data=data,
                conflicts=request.app.state.binding_conflicts)


@router.get("/ui/_/datasets/{dataset_id}/edit")
def edit(request: Request, dataset_id: str):
    _require_page_bindings(request)
    with db.reading(bind_of(request)) as session:
        row = repo.dataset_binding(session, dataset_id)
    if row is None or row.source != "page":
        raise HTTPException(status_code=404)
    return fragment(request, "partials/dataset_form.html", dataset_id=dataset_id,
                    location=row.location, visa_value=row.visa_value or "",
                    button="Save and sign", field=f"edit-{dataset_id}")


@router.post("/ui/_/datasets/bind")
def bind(request: Request, dataset_id: str = Form(...),
         location: Optional[str] = Form(None),
         visa_value: Optional[str] = Form(None)):
    _require_page_bindings(request)
    state = request.app.state

    def work(session):
        datasets.bind(session, state.keys, node=state.settings.node_name,
                      root=state.settings.local_root, dataset_id=dataset_id,
                      location=location, visa_value=visa_value,
                      user=request.state.user)
    return act(request, work, then="/ui/datasets")


@router.post("/ui/_/datasets/{dataset_id}/unbind")
def unbind(request: Request, dataset_id: str):
    _require_page_bindings(request)
    state = request.app.state

    def work(session):
        datasets.unbind(session, state.keys, node=state.settings.node_name,
                        dataset_id=dataset_id, user=request.state.user)
    return act(request, work, then="/ui/datasets")
