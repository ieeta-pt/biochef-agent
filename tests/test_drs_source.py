"""Resolving a GA4GH DRS URI into a run's workspace (#21, F2).

A DRS URI names its own host, and the URI comes from the client. That is the
whole of the difficulty, and everything below follows from it: a service that
follows any host a workflow names is a request generator aimed at whatever it
can reach from inside the network it sits in, which in a TRE is the point of the
TRE.

Nothing here touches the network. The transport is injected, the same way the
passport work injects its key sets, so these are about what this service decides
rather than about whether urllib works.
"""

import hashlib
import io
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras_mod = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import pytest

from datasource import DataSourceError, DrsSource, _open_url
from workspace import make_workspace

HOST = "drs.example.test"
PAYLOAD = b">seq\nACGTACGTACGT\n"
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()
OBJECT_URL = f"https://{HOST}/ga4gh/drs/v1/objects/obj-1"
BYTES_URL = "https://bytes.example.test/obj-1"


@contextmanager
def workspace(tmp_path):
    """make_workspace returns a Workspace, which is not a context manager."""
    ws = make_workspace(root=str(tmp_path))
    try:
        yield ws
    finally:
        ws.cleanup()


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def transport(objects=None, payloads=None, seen=None):
    """Stands in for urlopen, recording every URL this service decides to open."""
    objects = objects or {}
    payloads = payloads or {}

    def open_url(url, headers):
        if seen is not None:
            seen.append(url)
        if url in objects:
            return _Body(json.dumps(objects[url]).encode())
        if url in payloads:
            return _Body(payloads[url])
        raise DataSourceError(f"the test transport was asked for {url}")

    return open_url


def an_object(**overrides):
    document = {
        "id": "obj-1",
        "size": len(PAYLOAD),
        "checksums": [{"type": "sha-256", "checksum": SHA256}],
        "access_methods": [{"type": "https", "access_url": {"url": BYTES_URL}}],
    }
    document.update(overrides)
    return document


def a_source(**kwargs):
    kwargs.setdefault("hosts", HOST)
    kwargs.setdefault("open_url", transport())
    return DrsSource(**kwargs)


# --- the host is the client's choice ----------------------------------------

def test_no_allowlist_refuses_to_start():
    """The same reasoning as localpath's root, and a stronger case: the client
    chooses the host, not merely the path."""
    with pytest.raises(ValueError) as caught:
        DrsSource(hosts="")
    assert "BIOCHEF_DRS_HOSTS" in str(caught.value)


def test_a_host_outside_the_allowlist_is_refused(tmp_path):
    source = a_source()
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError) as caught:
            source.fetch(ws, "in.fa", "drs://elsewhere.example.test/obj-1")
        assert "elsewhere.example.test" in str(caught.value)


def test_nothing_is_opened_for_a_refused_host(tmp_path):
    """The refusal must come before the request, or the allowlist documents a
    policy this service has already violated by the time it applies it."""
    seen = []
    source = a_source(open_url=transport(seen=seen))
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError):
            source.fetch(ws, "in.fa", "drs://elsewhere.example.test/obj-1")
    assert seen == []


def test_a_compact_identifier_is_refused(tmp_path):
    """Resolving one means asking a third-party resolver which host to contact,
    and taking an endpoint out of a document and then trusting it completely is
    a mistake this codebase has already made twice."""
    source = a_source()
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError) as caught:
            source.fetch(ws, "in.fa", "drs://ncbi.sra:SRR123")
        assert "compact identifier" in str(caught.value)


def test_an_object_id_that_climbs_the_path_is_refused(tmp_path):
    """The id becomes a path segment on the DRS server."""
    source = a_source()
    with workspace(tmp_path) as ws:
        for uri in (f"drs://{HOST}/../../admin", f"drs://{HOST}/a/b", f"drs://{HOST}/"):
            with pytest.raises(DataSourceError):
                source.fetch(ws, "in.fa", uri)


def test_something_that_is_not_a_drs_uri_is_refused(tmp_path):
    source = a_source()
    with workspace(tmp_path) as ws:
        for spec in (f"https://{HOST}/obj-1", "obj-1", 7, None, ""):
            with pytest.raises(DataSourceError):
                source.fetch(ws, "in.fa", spec)


# --- the spec's shapes -------------------------------------------------------

def test_an_object_with_a_direct_url_is_fetched(tmp_path):
    seen = []
    source = a_source(open_url=transport({OBJECT_URL: an_object()},
                                         {BYTES_URL: PAYLOAD}, seen))
    with workspace(tmp_path) as ws:
        source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert (Path(ws.path) / "in.fa").read_bytes() == PAYLOAD
    assert seen[0] == OBJECT_URL, (
        "the object endpoint must be the spec's: https, under /ga4gh/drs/v1"
    )


def test_an_access_id_is_resolved_through_the_access_endpoint(tmp_path):
    objects = {
        OBJECT_URL: an_object(access_methods=[{"type": "https", "access_id": "aid-9"}]),
        f"{OBJECT_URL}/access/aid-9": {"url": BYTES_URL},
    }
    source = a_source(open_url=transport(objects, {BYTES_URL: PAYLOAD}))
    with workspace(tmp_path) as ws:
        source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert (Path(ws.path) / "in.fa").read_bytes() == PAYLOAD


def test_headers_from_the_access_url_are_sent(tmp_path):
    """DRS gives them as "Name: value" strings, and a presigned URL is often
    useless without them."""
    sent = {}

    def open_url(url, headers):
        if url == BYTES_URL:
            sent.update(headers or {})
            return _Body(PAYLOAD)
        return _Body(json.dumps(an_object(access_methods=[{
            "type": "https",
            "access_url": {"url": BYTES_URL,
                           "headers": ["Authorization: Bearer xyz", "X-Trace: 1"]},
        }])).encode())

    source = a_source(open_url=open_url)
    with workspace(tmp_path) as ws:
        source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
    assert sent.get("Authorization") == "Bearer xyz"
    assert sent.get("X-Trace") == "1"


def test_an_object_with_no_access_methods_is_refused(tmp_path):
    source = a_source(open_url=transport({OBJECT_URL: an_object(access_methods=[])}))
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError):
            source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")


# --- what arrives is what was named ------------------------------------------

def test_bytes_that_do_not_match_the_declared_checksum_are_refused(tmp_path):
    """The gap C1 exists to close, and it does not stop being one because a
    different protocol opened it."""
    source = a_source(open_url=transport({OBJECT_URL: an_object()},
                                         {BYTES_URL: b"something else entirely"}))
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError) as caught:
            source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert "not the ones named" in str(caught.value)


def test_md5_is_accepted_when_that_is_all_the_server_offers(tmp_path):
    source = a_source(open_url=transport(
        {OBJECT_URL: an_object(checksums=[{"type": "md5", "checksum": MD5}])},
        {BYTES_URL: PAYLOAD}))
    with workspace(tmp_path) as ws:
        source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert (Path(ws.path) / "in.fa").read_bytes() == PAYLOAD


def test_sha256_is_preferred_over_md5(tmp_path):
    """Both offered, and a wrong md5 must not matter."""
    source = a_source(open_url=transport(
        {OBJECT_URL: an_object(checksums=[
            {"type": "md5", "checksum": "0" * 32},
            {"type": "sha-256", "checksum": SHA256}])},
        {BYTES_URL: PAYLOAD}))
    with workspace(tmp_path) as ws:
        source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")


def test_an_object_declaring_no_checksum_we_know_is_refused(tmp_path):
    source = a_source(open_url=transport(
        {OBJECT_URL: an_object(checksums=[{"type": "crc32c", "checksum": "abcd"}])},
        {BYTES_URL: PAYLOAD}))
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError) as caught:
            source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert "cannot be checked" in str(caught.value)


def test_a_server_sending_far_more_than_it_declared_is_cut_off(tmp_path):
    """A declared size of a few bytes and an endless body would otherwise fill
    the disk, and the workspace writer has no reason to know what was claimed."""
    source = a_source(open_url=transport({OBJECT_URL: an_object(size=16)},
                                         {BYTES_URL: b"x" * (4 * 1024 * 1024)}))
    with workspace(tmp_path) as ws:
        with pytest.raises(DataSourceError) as caught:
            source.fetch(ws, "in.fa", f"drs://{HOST}/obj-1")
        assert "more than" in str(caught.value)


def test_a_plain_http_url_is_never_opened():
    with pytest.raises(DataSourceError):
        _open_url("http://bytes.example.test/obj-1", {})
