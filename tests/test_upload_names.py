"""An uploaded filename is refused unless it is a plain file name (#39).

The commit before this one recorded what happened previously: `../ESCAPED.txt`
wrote outside the working directory with HTTP 200, an absolute path ignored the
working directory entirely, and both landed even when the request carried
nothing that could be parsed as a workflow -- because the upload loop runs
before `json.loads`. Each test here is the closed form of one of those.

Self-contained on purpose. `convert.py` builds an ORAS client and calls
`login()` at import time, so importing `main` reaches the registry; the stub
below prevents that. It is inline rather than in a conftest because this file
has to work on `master`, where there is no test harness yet -- the harness
arrives separately and the two must not collide.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub the registry before `main` is imported, and only the registry: FastAPI
# has to stay real, because TestClient drives it.
if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import pytest
from fastapi.testclient import TestClient

import main
from workspace import SAFE_NAME, UnsafeName, check_name


EMPTY_WORKFLOW = b'{"nodes": [], "edges": []}'


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Drive the handler with its runs rooted under tmp_path.

    The workspace is created under BIOCHEF_RUN_ROOT, so pointing that at
    tmp_path means a name that escaped would land somewhere this test can see.
    snakemake is stubbed out: these tests are about names, and invoking a real
    workflow engine to check one would make them slow and conditional.
    """
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "run_snakemake", lambda ws, *a, **k: (0, "", ""))
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, filename, content=b"payload", workflow=EMPTY_WORKFLOW):
    return client.post(
        "/convert",
        data={"biochef_workflow": workflow},
        files=[("files", (filename, content, "text/plain"))],
    )


# --------------------------------------------------------------------------
# the rule


REFUSED = [
    ("../ESCAPED.txt", "parent traversal"),
    ("../../ESCAPED.txt", "further up"),
    ("/etc/PWNED", "absolute, which cwd never constrained"),
    ("sub/../../x", "traversal hidden mid-path"),
    ("sub/x", "a subdirectory is still more than one component"),
    ("..", "the parent itself"),
    (".", "the directory itself"),
    ("", "empty, which resolves to the directory"),
    (".hidden", "a dotfile"),
    ("-rf", "reads as an option to whatever receives it"),
    ("evil-out\n", "a newline, which would add a line to the Snakefile"),
    ("a" * 129, "longer than any generated name"),
    ("café-in", "outside the generated character set"),
    (None, "starlette types filename as str | None"),
]


@pytest.mark.parametrize("name,why", REFUSED, ids=[r[1] for r in REFUSED])
def test_a_name_that_is_not_a_plain_file_name_is_refused(name, why):
    with pytest.raises(UnsafeName):
        check_name(name)


def test_the_names_the_converter_generates_are_accepted():
    """The rule has to admit everything the system actually produces.

    A node id is "{operation.id}-{timestamp}" and a slot is
    f"{source}-{source_handle}", so these are the real shapes.
    """
    for name in ["input-1-out", "tn93.distance-1-out", "Snakefile",
                 "gto.fasta.extract-1730000000000-out", "intermediate.json"]:
        assert check_name(name) == name


def test_the_anchors_reject_a_trailing_newline():
    r"""Why \A and \Z rather than ^ and $.

    With re.match, `$` also matches just before a trailing newline, so the
    obvious spelling of this pattern would accept "evil-out\n" -- and a newline
    is the one character that would let a name write an extra line into the
    generated Snakefile.
    """
    import re
    caret_dollar = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    assert caret_dollar.match("evil-out\n"), "this is the trap being avoided"
    assert not SAFE_NAME.match("evil-out\n")


# --------------------------------------------------------------------------
# through the handler


def test_a_relative_name_no_longer_escapes(client, tmp_path):
    response = post(client, "../ESCAPED.txt")

    assert response.status_code == 400
    assert not list(tmp_path.rglob("ESCAPED.txt"))


def test_an_absolute_name_no_longer_escapes(client, tmp_path):
    target = tmp_path / "elsewhere" / "ABSOLUTE.txt"
    target.parent.mkdir()

    response = post(client, str(target))

    assert response.status_code == 400
    assert not target.exists()


def test_a_malformed_body_is_refused_before_anything_is_written(client, tmp_path):
    """The parse now runs first (#40), so a request that is not a workflow never
    reaches the upload loop at all. The name check still stands on its own --
    see the table above -- but the ordering means it is no longer the only
    thing between a request and a write."""
    response = post(client, "input-1-out", workflow=b"this is not json at all")

    assert response.status_code >= 400
    assert not list(tmp_path.rglob("input-1-out")), "nothing was written"


def test_a_plain_name_still_works(client, tmp_path, monkeypatch):
    """The case that must keep working: the fix cannot be "reject everything"."""
    kept = []
    real = main.make_workspace
    monkeypatch.setattr(main, "make_workspace",
                        lambda root=None: kept.append(real(root)) or kept[-1])
    monkeypatch.setattr(main, "KEEP_WORKSPACE", True)

    response = post(client, "input-1-out")

    assert response.status_code == 200, response.text
    assert (Path(kept[0].path) / "input-1-out").read_bytes() == b"payload"
    kept[0].cleanup()


@pytest.mark.parametrize("name", ["Snakefile", "SNAKEFILE", "snakefile"])
def test_an_upload_occupying_the_snakefile_slot_is_a_bad_request(name, client, tmp_path):
    """400, not 500.

    "Snakefile" is a perfectly legal single path component, so the shape rule
    passes it and O_EXCL then refuses the generated write. That refusal is
    correct -- the attacker's file is never executed, and run_snakemake is never
    reached -- but without the mapping it surfaced as an unhandled exception for
    what is a bad request.

    The case variants matter on macOS: APFS is case-insensitive by default, so
    "SNAKEFILE" occupies the same slot.
    """
    response = post(client, name)

    assert response.status_code == 400, response.text
    assert "Snakefile" in response.text


def test_an_upload_cannot_shadow_a_file_the_run_already_made(client, tmp_path):
    """O_EXCL. Sending the same name twice is refused rather than overwriting,
    which is also what stops an upload landing on a tool binary."""
    response = client.post(
        "/convert",
        data={"biochef_workflow": EMPTY_WORKFLOW},
        files=[("files", ("same-name", b"first", "text/plain")),
               ("files", ("same-name", b"second", "text/plain"))],
    )

    assert response.status_code == 400
    assert "sent twice" in response.text
