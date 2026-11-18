"""Where a dataset lives on this node (section 6.6).

A study names datasets by id, and a binding says which folder under
BIOCHEF_LOCAL_ROOT holds one. Bindings come from a config file, which is locked,
or from the page, which signs them. The config always wins: whoever runs the
machine has the last word on what is where.

A binding also carries the dataset's visa value, the exact string the data access
committee issues in a visa for it, usually a URL. A study's dataset id is this
node's own name for the data and visas do not use it, so the binding is what
connects the two. It decides who gets in, so it is signed with the location.

A binding names a folder, and a run reads files. `file_in_binding` is how one
reaches the other: a request names a file relative to the dataset, and this turns
that into the path LocalPathSource expects, refusing anything that climbs out of
the dataset's own folder. Staying under the root would not be enough, because the
root holds every other study's data too.
"""

import hashlib
import os
import re
from typing import NamedTuple, Optional

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
VISA_VALUE_LIMIT = 2048


class Entry(NamedTuple):
    """What a config file says about one dataset."""
    location: str
    visa_value: Optional[str] = None


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


def check_visa_value(value):
    """The value to store, or None when there is none. Never rewritten.

    A visa is compared with this exactly, so a value is kept as it was issued. Only
    the space a form or a YAML file may add around it goes.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise BindingError("A visa value is text.")
    value = value.strip()
    if not value:
        return None
    if len(value) > VISA_VALUE_LIMIT:
        raise BindingError(f"A visa value has at most {VISA_VALUE_LIMIT} characters.")
    if not value.isprintable():
        raise BindingError("A visa value cannot hold control characters.")
    return value


def file_in_binding(root, location, relative):
    """A path for LocalPathSource: `relative`, inside this dataset's folder only."""
    folder = check_location(root, location)
    try:
        target = resolve_under_root(folder, relative)
    except DataSourceError:
        raise BindingError(f"{relative!r} is outside this dataset.") from None
    return os.path.relpath(target, os.path.realpath(root))


def load_file(path, root):
    """The bindings a config file declares: {dataset id: Entry}. Or a refusal."""
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
            visa_value = check_visa_value(entry.get("visa_value"))
        except BindingError as refused:
            raise BindingError(f"{path}: {dataset_id}: {refused}") from None
        entries[dataset_id] = Entry(location.strip(), visa_value)
    return entries


def _hash(location, visa_value):
    data = decisions.canonical({"location": location, "visa_value": visa_value})
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _describe(location, visa_value):
    return f"location {location}, " + (f"visa value {visa_value}" if visa_value
                                       else "no visa value")


def _signed(session, keys, node, action, dataset_id, location, visa_value, user, now,
            reason=None):
    return decisions.decide(
        session, keys, node=node, subject_type=SubjectType.DATASET_BINDING,
        subject_id=dataset_id, subject_hash=_hash(location, visa_value), action=action,
        user=user, at=now, reason=reason or _describe(location, visa_value))


def sync(session, keys, node, entries, now=None):
    """Make the store match the config file. Returns the page bindings it replaced.

    A page binding the file now covers loses, with a signed unbind so that the
    change is on the record, and the page reports each one. A config binding that
    left the file goes too, since nothing vouches for it any more.
    """
    now = now or repo.utcnow()
    replaced = []
    for row in repo.dataset_bindings(session):
        entry = entries.get(row.dataset_id)
        if (row.source == BindingSource.PAGE.value and entry is not None
                and (row.location, row.visa_value) != tuple(entry)):
            replaced.append({"dataset_id": row.dataset_id,
                             "page": _describe(row.location, row.visa_value),
                             "config": _describe(*entry),
                             "bound_by": row.bound_by})
            _signed(session, keys, node, Action.UNBIND, row.dataset_id, row.location,
                    row.visa_value, CONFIG_USER, now,
                    "the config file binds this dataset")
        if row.source == BindingSource.CONFIG.value and row.dataset_id not in entries:
            repo.drop_config_binding(session, row.dataset_id)
    for dataset_id, entry in entries.items():
        repo.bind_dataset(session, dataset_id=dataset_id, source=BindingSource.CONFIG,
                          type=LOCALPATH, location=entry.location,
                          visa_value=entry.visa_value, bound_by=CONFIG_USER, now=now)
    session.flush()
    return replaced


def _named_by_a_study(session, dataset_id):
    return any(dataset_id in row.datasets for row in repo.open_versions(session))


def bind(session, keys, *, node, root, dataset_id, location, user, visa_value=None,
         now=None):
    """Bind or rebind from the page. Signed, audited, and never over the config."""
    now = now or repo.utcnow()
    if not DATASET_ID.fullmatch(dataset_id or ""):
        raise BindingError("That is not a dataset id.")
    if not _named_by_a_study(session, dataset_id):
        raise BindingError(f"No pending or approved study names {dataset_id}.")
    check_location(root, location)
    location = location.strip()
    visa_value = check_visa_value(visa_value)
    row = repo.bind_dataset(session, dataset_id=dataset_id, source=BindingSource.PAGE,
                            type=LOCALPATH, location=location, visa_value=visa_value,
                            bound_by=user, now=now)
    _signed(session, keys, node, Action.BIND, dataset_id, location, visa_value, user,
            now)
    return row


def unbind(session, keys, *, node, dataset_id, user, now=None):
    now = now or repo.utcnow()
    row = repo.dataset_binding(session, dataset_id)
    if row is None:
        raise Conflict(f"{dataset_id} is not bound.")
    location, visa_value = row.location, row.visa_value
    repo.unbind_dataset(session, dataset_id)
    _signed(session, keys, node, Action.UNBIND, dataset_id, location, visa_value, user,
            now)
