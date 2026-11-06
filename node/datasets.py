"""Where a dataset lives on this node (section 6.6).

A study names datasets by id, and a binding says which folder under
BIOCHEF_LOCAL_ROOT holds one. Bindings come from a config file, which is locked,
or from the page, which signs them. The config always wins: whoever runs the
machine has the last word on what is where.

A binding names a folder, and a run reads files. `file_in_binding` is how one
reaches the other: a request names a file relative to the dataset, and this turns
that into the path LocalPathSource expects, refusing anything that climbs out of
the dataset's own folder. Staying under the root would not be enough, because the
root holds every other study's data too.
"""

import hashlib
import os
import re

import yaml

from datasource import DataSourceError, resolve_under_root
from node import decisions
from store import repo
from store.errors import Conflict
from store.states import Action, BindingSource, SubjectType

LOCALPATH = "localpath"
CONFIG_USER = "config file"
"""Who a change made by the config file is recorded under. No person made it."""

DATASET_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class BindingError(Exception):
    """A binding that cannot be accepted. The message is for the page."""


def check_location(root, location):
    """The folder a location means, or a refusal. Shared with LocalPathSource."""
    if not isinstance(location, str) or not location.strip():
        raise BindingError("A binding needs a folder under the data root.")
    try:
        folder = resolve_under_root(root, location.strip())
    except DataSourceError:
        raise BindingError(f"{location!r} resolves outside the data root.") from None
    if not os.path.isdir(folder):
        raise BindingError(f"{location!r} is not a folder under the data root.")
    return folder


def file_in_binding(root, location, relative):
    """A path for LocalPathSource: `relative`, inside this dataset's folder only."""
    folder = check_location(root, location)
    try:
        target = resolve_under_root(folder, relative)
    except DataSourceError:
        raise BindingError(f"{relative!r} is outside this dataset.") from None
    return os.path.relpath(target, os.path.realpath(root))


def load_file(path, root):
    """The bindings a config file declares: {dataset id: location}. Or a refusal."""
    try:
        with open(path, "r") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as failure:
        raise BindingError(f"{path} cannot be read: {failure}") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise BindingError(f"{path} must map dataset ids to bindings")
    entries = {}
    for dataset_id, entry in document.items():
        if not isinstance(dataset_id, str) or not DATASET_ID.fullmatch(dataset_id):
            raise BindingError(f"{path}: {dataset_id!r} is not a dataset id")
        if not isinstance(entry, dict) or entry.get("type") != LOCALPATH:
            raise BindingError(f"{path}: {dataset_id} needs type: {LOCALPATH}")
        location = entry.get("location")
        try:
            check_location(root, location)
        except BindingError as refused:
            raise BindingError(f"{path}: {dataset_id}: {refused}") from None
        entries[dataset_id] = location.strip()
    return entries


def _hash(location):
    return "sha256:" + hashlib.sha256(location.encode()).hexdigest()


def _signed(session, keys, node, action, dataset_id, location, user, now, reason):
    return decisions.decide(
        session, keys, node=node, subject_type=SubjectType.DATASET_BINDING,
        subject_id=dataset_id, subject_hash=_hash(location), action=action,
        user=user, at=now, reason=reason)


def sync(session, keys, node, entries, now=None):
    """Make the store match the config file. Returns the page bindings it replaced.

    A page binding the file now covers loses, with a signed unbind so that the
    change is on the record, and the page reports each one. A config binding that
    left the file goes too, since nothing vouches for it any more.
    """
    now = now or repo.utcnow()
    replaced = []
    for row in repo.dataset_bindings(session):
        if (row.source == BindingSource.PAGE.value and row.dataset_id in entries
                and row.location != entries[row.dataset_id]):
            replaced.append({"dataset_id": row.dataset_id, "page": row.location,
                             "config": entries[row.dataset_id],
                             "bound_by": row.bound_by})
            _signed(session, keys, node, Action.UNBIND, row.dataset_id, row.location,
                    CONFIG_USER, now, "the config file binds this dataset")
        if row.source == BindingSource.CONFIG.value and row.dataset_id not in entries:
            repo.drop_config_binding(session, row.dataset_id)
    for dataset_id, location in entries.items():
        repo.bind_dataset(session, dataset_id=dataset_id, source=BindingSource.CONFIG,
                          type=LOCALPATH, location=location, bound_by=CONFIG_USER,
                          now=now)
    session.flush()
    return replaced


def _named_by_a_study(session, dataset_id):
    return any(dataset_id in row.datasets for row in repo.open_versions(session))


def bind(session, keys, *, node, root, dataset_id, location, user, now=None):
    """Bind or rebind from the page. Signed, audited, and never over the config."""
    now = now or repo.utcnow()
    if not DATASET_ID.fullmatch(dataset_id or ""):
        raise BindingError("That is not a dataset id.")
    if not _named_by_a_study(session, dataset_id):
        raise BindingError(f"No pending or approved study names {dataset_id}.")
    check_location(root, location)
    location = location.strip()
    row = repo.bind_dataset(session, dataset_id=dataset_id, source=BindingSource.PAGE,
                            type=LOCALPATH, location=location, bound_by=user, now=now)
    _signed(session, keys, node, Action.BIND, dataset_id, location, user, now,
            f"location {location}")
    return row


def unbind(session, keys, *, node, dataset_id, user, now=None):
    now = now or repo.utcnow()
    row = repo.dataset_binding(session, dataset_id)
    if row is None:
        raise Conflict(f"{dataset_id} is not bound.")
    location = row.location
    repo.unbind_dataset(session, dataset_id)
    _signed(session, keys, node, Action.UNBIND, dataset_id, location, user, now,
            f"location {location}")
