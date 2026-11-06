"""The Datasets screen (mock 06), and what it refuses."""

import re

import pytest

from node import datasets
from store import db, repo, seed
from store.fixtures import mock
from store.states import BindingSource
from tests.store_support import fresh_database
from tests.ui_support import KEYS, client_for, dev_settings


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "root"
    (folder / "rare" / "pilot_v1").mkdir(parents=True)
    return str(folder)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    return bind


def client(engine, root, from_ui=True):
    return client_for(dev_settings(node_name=mock.NODE, local_root=root,
                                   dev_user="j.silva", bindings_from_ui=from_ui),
                      bind=engine)


def post(client, path, **data):
    page = client.get("/ui/inbox").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return client.post(path, data=data, headers={"X-CSRF-Token": token})


def test_the_table_of_the_mock(engine, root):
    text = client(engine, root).get("/ui/datasets").text
    assert "EGAD00001009912" in text and "cardio/2019_2024/" in text
    assert "config file, locked" in text
    assert "this page · j.silva" in text
    assert "no approved study" in text
    assert "cardio-pt · CARDIO-PT cohort · v3 (in Inbox)" in text


def test_a_locked_row_has_no_controls(engine, root):
    text = client(engine, root).get("/ui/datasets").text
    assert text.count(">Edit</button>") == 1 and text.count(">Remove</button>") == 1


def test_the_waiting_card_of_the_mock(engine, root):
    text = client(engine, root).get("/ui/datasets").text
    assert "Waiting for a binding" in text
    assert ("EGAD00001010247 is named by rare-variants-pilot · Rare variants pilot · "
            "v1, in the Inbox.") in text
    assert ">Bind and sign</button>" in text
    assert "The agent refuses anything outside it." in text


def test_the_switch_hides_every_form(engine, root):
    text = client(engine, root, from_ui=False).get("/ui/datasets").text
    assert "Bindings on this node come only from the config file." in text
    for hidden in ("Waiting for a binding", ">Bind and sign<", ">Edit<", ">Remove<",
                   "also allows bindings"):
        assert hidden not in text


def test_the_switch_also_closes_the_routes(engine, root):
    web = client(engine, root, from_ui=False)
    assert post(web, "/ui/_/datasets/bind", dataset_id=mock.RARE_DATASET,
                location="rare/pilot_v1").status_code == 403
    assert post(web, f"/ui/_/datasets/{mock.MICROBIOME_DATASET}/unbind").status_code == 403
    assert web.get(f"/ui/_/datasets/{mock.MICROBIOME_DATASET}/edit").status_code == 403


def test_binding_from_the_page(engine, root):
    web = client(engine, root)
    response = post(web, "/ui/_/datasets/bind", dataset_id=mock.RARE_DATASET,
                    location="rare/pilot_v1")
    assert response.headers["hx-redirect"] == "/ui/datasets"
    text = web.get("/ui/datasets").text
    assert "Waiting for a binding" not in text
    assert "rare/pilot_v1" in text
    assert "✓ bound to local data" in web.get("/ui/inbox/study/4").text


def test_outside_the_root_is_refused_in_place(engine, root):
    response = post(client(engine, root), "/ui/_/datasets/bind",
                    dataset_id=mock.RARE_DATASET, location="../../etc")
    assert response.status_code == 409
    assert "outside the data root" in response.text


def test_the_config_wins_in_place(engine, root):
    response = post(client(engine, root), "/ui/_/datasets/bind",
                    dataset_id=mock.CARDIO_DATASET, location="rare/pilot_v1")
    assert response.status_code == 409
    assert "config file" in response.text


def test_edit_offers_the_current_folder(engine, root):
    part = client(engine, root).get(f"/ui/_/datasets/{mock.MICROBIOME_DATASET}/edit").text
    assert 'value="micro/16s_2023/"' in part and ">Save and sign</button>" in part


def test_a_config_row_cannot_be_edited(engine, root):
    web = client(engine, root)
    assert web.get(f"/ui/_/datasets/{mock.CARDIO_DATASET}/edit").status_code == 404


def test_remove(engine, root):
    web = client(engine, root)
    response = post(web, f"/ui/_/datasets/{mock.MICROBIOME_DATASET}/unbind")
    assert response.status_code == 200
    with db.reading(engine) as session:
        assert repo.dataset_binding(session, mock.MICROBIOME_DATASET) is None


def test_a_conflict_at_start_is_shown(engine, root, tmp_path):
    web = client(engine, root)
    web.app.state.binding_conflicts = [{"dataset_id": "EGAD1", "page": "a",
                                        "config": "b", "bound_by": "j.silva"}]
    text = web.get("/ui/datasets").text
    assert "the config file wins" in text and 'role="alert"' in text


def test_an_empty_node_says_so(tmp_path, root):
    (tmp_path / "x").mkdir()
    empty = fresh_database(tmp_path / "x")[1]
    assert "No dataset is bound on this node." in client(empty, root).get(
        "/ui/datasets").text
