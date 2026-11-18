"""Bindings: under the root, signed, and always behind the config file (6.6)."""

import os

import pytest

import audit
import datasource
from node import datasets, decisions
from node.datasets import BindingError
from node.keys import generate
from store import db, repo
from store.errors import Conflict
from store.states import BindingSource, SubjectType
from tests.store_support import add_version, fresh_database

KEYS = generate()
NODE = "node-a"
VALUE = "https://dac.example.org/datasets/EGAD1"
OTHER_VALUE = "https://dac.example.org/datasets/EGAD1/v2"


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "root"
    (folder / "cohort" / "sub").mkdir(parents=True)
    (folder / "cohort" / "sub" / "reads.vcf").write_text("x")
    (folder / "other").mkdir()
    (folder / "other" / "secret.vcf").write_text("x")
    (tmp_path / "outside").mkdir()
    return str(folder)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    with db.writing(bind) as session:
        add_version(session)
    return bind


def page_bind(engine, root, location="cohort", dataset="EGAD1", user="j.silva",
              visa_value=None):
    with db.writing(engine) as session:
        return datasets.bind(session, KEYS, node=NODE, root=root, dataset_id=dataset,
                             location=location, user=user, visa_value=visa_value)


def binding(engine, dataset="EGAD1"):
    with db.reading(engine) as session:
        return repo.dataset_binding(session, dataset)


def about(engine, dataset="EGAD1"):
    with db.reading(engine) as session:
        return repo.decisions_about(session, SubjectType.DATASET_BINDING, dataset)


# The root


@pytest.mark.parametrize("location", ["..", "../outside", "cohort/../..", "/etc",
                                      "/"])
def test_anything_outside_the_root_is_refused(root, location):
    with pytest.raises(BindingError, match="outside the data root"):
        datasets.check_location(root, location)


def test_a_link_out_of_the_root_is_refused(root, tmp_path):
    os.symlink(tmp_path / "outside", os.path.join(root, "escape"))
    with pytest.raises(BindingError, match="outside the data root"):
        datasets.check_location(root, "escape")


def test_a_sibling_that_starts_with_the_roots_name_is_outside(root, tmp_path):
    (tmp_path / "root-other").mkdir()
    with pytest.raises(BindingError, match="outside the data root"):
        datasets.check_location(root, "../root-other")


def test_a_path_with_a_nul_is_refused(root):
    with pytest.raises(BindingError):
        datasets.check_location(root, "cohort\x00")


@pytest.mark.parametrize("location", ["", "   ", None])
def test_a_binding_needs_a_folder(root, location):
    with pytest.raises(BindingError, match="needs a folder"):
        datasets.check_location(root, location)


def test_a_file_or_a_missing_folder_is_not_a_binding(root):
    with pytest.raises(BindingError, match="not a folder"):
        datasets.check_location(root, "cohort/sub/reads.vcf")
    with pytest.raises(BindingError, match="not a folder"):
        datasets.check_location(root, "nowhere")


def test_a_folder_under_the_root_is_fine(root):
    assert datasets.check_location(root, "cohort/sub/").endswith("cohort/sub")


def test_localpath_and_bindings_share_the_one_rule(root):
    assert datasets.resolve_under_root is datasource.resolve_under_root


def test_localpath_still_refuses_what_it_refused(root, tmp_path):
    source = datasource.LocalPathSource(root)
    for spec, message in (("../outside", "resolves outside"), ("a\x00b", "NUL")):
        with pytest.raises(datasource.DataSourceError, match=message):
            source.fetch(None, "input", spec)


# A file in a bound folder


def test_a_file_in_the_dataset_becomes_a_localpath_path(root):
    assert datasets.file_in_binding(root, "cohort", "sub/reads.vcf") == (
        "cohort/sub/reads.vcf")


@pytest.mark.parametrize("relative", ["../other/secret.vcf", "/etc/passwd"])
def test_a_file_cannot_leave_the_datasets_own_folder(root, relative):
    """The neighbouring dataset is under the root too. That is not enough."""
    with pytest.raises(BindingError, match="outside this dataset"):
        datasets.file_in_binding(root, "cohort", relative)


def test_the_result_is_what_localpath_reads(root, tmp_path):
    class Workspace:
        def write_stream(self, name, stream):
            self.data = stream.read()
    workspace = Workspace()
    spec = datasets.file_in_binding(root, "cohort", "sub/reads.vcf")
    datasource.LocalPathSource(root).fetch(workspace, "input", spec)
    assert workspace.data == b"x"


# The config file


def write(tmp_path, text):
    path = tmp_path / "datasets.yaml"
    path.write_text(text)
    return str(path)


def test_a_config_file_loads(root, tmp_path):
    path = write(tmp_path, "EGAD1:\n  type: localpath\n  location: cohort/\n")
    assert datasets.load_file(path, root) == {"EGAD1": ("cohort/", None)}


def test_a_config_file_carries_the_visa_value(root, tmp_path):
    path = write(tmp_path, "EGAD1:\n  type: localpath\n  location: cohort/\n"
                           f"  visa_value: {VALUE}\n")
    assert datasets.load_file(path, root)["EGAD1"].visa_value == VALUE


def test_an_empty_config_file_binds_nothing(root, tmp_path):
    assert datasets.load_file(write(tmp_path, ""), root) == {}


@pytest.mark.parametrize("text,message", [
    ("- a\n", "must map"),
    ("EGAD1: cohort\n", "needs type"),
    ("EGAD1:\n  type: s3\n  location: cohort\n", "needs type"),
    ("EGAD1:\n  type: localpath\n", "needs a folder"),
    ("EGAD1:\n  type: localpath\n  location: ../outside\n", "outside the data root"),
    ("'bad id':\n  type: localpath\n  location: cohort\n", "not a dataset id"),
    ("a: [\n", "cannot be read"),
    ("EGAD1:\n  type: localpath\n  location: cohort\n  visa_value: [1]\n", "is text"),
])
def test_a_bad_config_file_is_refused(root, tmp_path, text, message):
    with pytest.raises(BindingError, match=message):
        datasets.load_file(write(tmp_path, text), root)


def test_a_missing_config_file_is_refused(root, tmp_path):
    with pytest.raises(BindingError, match="cannot be read"):
        datasets.load_file(str(tmp_path / "none.yaml"), root)


def sync(engine, entries):
    with db.writing(engine) as session:
        return datasets.sync(session, KEYS, NODE, {
            dataset: datasets.Entry(entry) if isinstance(entry, str) else entry
            for dataset, entry in entries.items()})


def test_syncing_binds_and_locks(engine):
    sync(engine, {"EGAD1": "cohort"})
    row = binding(engine)
    assert (row.source, row.location, row.bound_by) == ("config", "cohort", "config file")


def test_the_config_wins_over_the_page_and_says_so(engine, root):
    page_bind(engine, root, "other")
    replaced = sync(engine, {"EGAD1": "cohort"})
    assert replaced == [{"dataset_id": "EGAD1",
                         "page": "location other, no visa value",
                         "config": "location cohort, no visa value",
                         "bound_by": "j.silva"}]
    assert binding(engine).source == "config"
    assert about(engine)[0].action == "unbind"
    assert about(engine)[0].user == "config file"


def test_a_config_binding_that_left_the_file_goes(engine):
    sync(engine, {"EGAD1": "cohort"})
    sync(engine, {})
    assert binding(engine) is None


def test_a_page_binding_the_file_does_not_cover_stays(engine, root):
    page_bind(engine, root)
    assert sync(engine, {}) == []
    assert binding(engine).source == "page"


def test_syncing_twice_changes_nothing(engine):
    sync(engine, {"EGAD1": "cohort"})
    assert sync(engine, {"EGAD1": "cohort"}) == []
    assert about(engine) == []


def test_syncing_stores_the_visa_value(engine):
    sync(engine, {"EGAD1": datasets.Entry("cohort", VALUE)})
    assert binding(engine).visa_value == VALUE


def test_a_visa_value_the_file_changed_is_stored(engine):
    sync(engine, {"EGAD1": datasets.Entry("cohort", VALUE)})
    sync(engine, {"EGAD1": datasets.Entry("cohort", OTHER_VALUE)})
    assert binding(engine).visa_value == OTHER_VALUE


def test_a_visa_value_alone_is_enough_for_the_config_to_replace_the_page(engine, root):
    page_bind(engine, root, "cohort", visa_value=VALUE)
    replaced = sync(engine, {"EGAD1": datasets.Entry("cohort", OTHER_VALUE)})
    assert replaced[0]["page"] == f"location cohort, visa value {VALUE}"
    assert replaced[0]["config"] == f"location cohort, visa value {OTHER_VALUE}"
    assert binding(engine).visa_value == OTHER_VALUE


def test_the_page_agreeing_with_the_file_loses_nothing(engine, root):
    page_bind(engine, root, "cohort", visa_value=VALUE)
    assert sync(engine, {"EGAD1": datasets.Entry("cohort", VALUE)}) == []


# From the page


def test_binding_from_the_page_is_signed_and_audited(engine, root, tmp_path):
    page_bind(engine, root, "cohort/")
    row = about(engine)[0]
    signed = decisions.verify(row.object_raw, row.signature, KEYS.signing.public_key())
    assert (signed["action"], signed["user"], signed["subject_id"]) == (
        "bind", "j.silva", "EGAD1")
    assert "cohort/" in signed["reason"]
    assert audit.read(str(tmp_path / "audit.jsonl"))[-1]["user"] == "j.silva"


def test_the_visa_value_is_signed_with_the_binding(engine, root):
    page_bind(engine, root, "cohort", visa_value=VALUE)
    row = about(engine)[0]
    signed = decisions.verify(row.object_raw, row.signature, KEYS.signing.public_key())
    assert VALUE in signed["reason"]
    assert binding(engine).visa_value == VALUE


def test_changing_only_the_visa_value_signs_a_different_hash(engine, root):
    page_bind(engine, root, "cohort", visa_value=VALUE)
    page_bind(engine, root, "cohort", visa_value=OTHER_VALUE)
    first, second = about(engine)[:2]
    assert first.subject_hash != second.subject_hash


def test_a_binding_with_no_visa_value_says_so(engine, root):
    page_bind(engine, root, "cohort")
    assert "no visa value" in about(engine)[0].reason
    assert binding(engine).visa_value is None


def test_unbinding_names_the_visa_value_that_went(engine, root):
    page_bind(engine, root, "cohort", visa_value=VALUE)
    with db.writing(engine) as session:
        datasets.unbind(session, KEYS, node=NODE, dataset_id="EGAD1", user="j.silva")
    assert any(row.action == "unbind" and VALUE in row.reason for row in about(engine))


@pytest.mark.parametrize("value,kept", [
    (None, None), ("", None), ("   ", None), (f"  {VALUE}\n", VALUE),
    ("https://x/a b?c=D", "https://x/a b?c=D"),
])
def test_a_visa_value_is_kept_as_issued(value, kept):
    assert datasets.check_visa_value(value) == kept


@pytest.mark.parametrize("value,message", [
    ("x" * 2049, "at most"), ("a\x00b", "control"), ("a\nb", "control"), (5, "is text"),
])
def test_a_bad_visa_value_is_refused(value, message):
    with pytest.raises(BindingError, match=message):
        datasets.check_visa_value(value)


def test_the_longest_visa_value_is_accepted():
    assert len(datasets.check_visa_value("x" * 2048)) == 2048


def test_the_page_cannot_override_the_config(engine, root):
    sync(engine, {"EGAD1": "cohort"})
    with pytest.raises(Conflict, match="config file"):
        page_bind(engine, root, "other")
    assert binding(engine).location == "cohort"


def test_the_page_cannot_remove_a_config_binding(engine, root):
    sync(engine, {"EGAD1": "cohort"})
    with pytest.raises(Conflict, match="config file"):
        with db.writing(engine) as session:
            datasets.unbind(session, KEYS, node=NODE, dataset_id="EGAD1", user="j.silva")
    assert binding(engine) is not None


def test_outside_the_root_is_refused_from_the_page(engine, root):
    with pytest.raises(BindingError, match="outside the data root"):
        page_bind(engine, root, "../outside")
    assert binding(engine) is None and about(engine) == []


def test_only_a_dataset_a_study_names_can_be_bound(engine, root):
    with pytest.raises(BindingError, match="No pending or approved study"):
        page_bind(engine, root, dataset="EGAD9")


@pytest.mark.parametrize("dataset", ["", "a b", "x" * 129, None])
def test_a_dataset_id_has_a_shape(engine, root, dataset):
    with pytest.raises(BindingError, match="not a dataset id"):
        page_bind(engine, root, dataset=dataset)


def test_rebinding_moves_it_and_signs_again(engine, root):
    page_bind(engine, root, "cohort")
    page_bind(engine, root, "other", user="a.costa")
    assert (binding(engine).location, binding(engine).bound_by) == ("other", "a.costa")
    assert len(about(engine)) == 2


def test_removing_is_signed(engine, root):
    page_bind(engine, root)
    with db.writing(engine) as session:
        datasets.unbind(session, KEYS, node=NODE, dataset_id="EGAD1", user="a.costa")
    assert binding(engine) is None
    assert (about(engine)[0].action, about(engine)[0].user) == ("unbind", "a.costa")


def test_removing_what_is_not_bound_is_refused(engine):
    with pytest.raises(Conflict, match="not bound"):
        with db.writing(engine) as session:
            datasets.unbind(session, KEYS, node=NODE, dataset_id="EGAD1", user="j.silva")


def test_a_failed_binding_writes_no_row_when_the_trail_is_down(engine, root, monkeypatch,
                                                               tmp_path):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "no" / "such" / "file"))
    with pytest.raises(audit.AuditError):
        page_bind(engine, root)
    assert binding(engine) is None
