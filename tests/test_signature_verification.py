"""What proves a bundle came from the Hub before we execute it (#14, E1b).

Recorded before anything changes. Nothing does, and the asymmetry is the point.

The Hub half of E1 already exists and is merged. `publish-recipes.yml` installs
cosign, runs `cosign sign` and `cosign attest` over every published bundle, and
then verifies its own work against a policy document:

    biochef.signing-policy.v1
      registry_prefix           registry.biochef.app/biochef-port-plugins-
      certificate_identity      .../publish-recipes.yml@refs/heads/...
      certificate_oidc_issuer   https://token.actions.githubusercontent.com
      slsa_*                    builder, predicate, build type, source repo/ref

The browser half exists too: Biochef#96 landed catalogue verification and
integrity checks on bundles.

The Agent verifies nothing. It resolves a manifest, pulls the blobs, checks them
against the digests that manifest declares -- which is C1, and which this branch
is built on -- and then chmods the binary 0700 and runs it.

Digest validation and signature verification answer different questions, and
having the first is not having the second:

    digest  : "are these the bytes the manifest named?"
    signature: "did anyone we trust ever vouch for this manifest?"

A registry that serves a manifest it made up passes the digest check perfectly,
because the blobs match the digests in the manifest it also made up. Nothing so
far distinguishes the Hub's artifact from any other well-formed one, and the
Agent is configured with a registry URL by an operator and pulls whatever `repo`
a workflow names.

That is what E1b closes, and why the roadmap calls it the gate for Milestone 3:
no TRE runs unverifiable binaries pulled from the network.
"""

import re
import sys
import types
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

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


def _repo_source():
    """Every module in the service, as text.

    Read rather than imported, for the reason test_digest_validation gives: the
    modules here install stubs in sys.modules, so what an import finds in a full
    run depends on which test module got there first. Reading cannot be shadowed.
    """
    return {p.name: p.read_text() for p in REPO_ROOT.glob("*.py")}


def test_the_scan_finds_the_modules_it_claims_to_read():
    """Guard against every assertion below passing over an empty set."""
    source = _repo_source()
    assert "convert.py" in source
    assert "fetch_tool" in source["convert.py"]


def test_nothing_in_the_service_verifies_a_signature():
    """The gap, stated as an assertion so it cannot be argued about.

    This test is expected to be INVERTED by the change that closes #14. It is
    here so that the commit which adds verification has to delete a statement
    that the service does not verify, rather than quietly adding a function
    nobody wired up.
    """
    source = _repo_source()
    for name, text in source.items():
        for evidence in ("cosign", "certificate_identity", "signing-policy"):
            assert evidence not in text, (
                f"{name} mentions {evidence!r}; if signature verification has "
                f"landed, this test should be replaced by one asserting that an "
                f"unsigned bundle does not execute"
            )


def test_the_digest_check_that_exists_does_not_answer_the_signature_question():
    """Both are needed, and the first is already here.

    verify_against_manifest compares staged files against the digests declared
    in the manifest it was handed. Every digest in that comparison comes from
    the same manifest, so a registry serving a self-consistent artifact of its
    own making passes it.
    """
    import convert

    body = _repo_source()["convert.py"]
    verify = body.split("def verify_against_manifest")[1].split("\ndef ")[0]

    assert "layer.get(\"digest\")" in verify, "read the wrong function"
    # Every digest compared comes from the manifest argument. Nothing external
    # to the manifest is consulted, which is exactly why it cannot establish
    # provenance.
    assert "policy" not in verify
    assert "signature" not in verify
    assert callable(convert.verify_against_manifest)


def test_no_setting_selects_a_verification_mode():
    """There is no switch, so there is no default to get wrong -- yet.

    When one arrives it has to be documented; test_settings_are_documented
    enforces that against README.md and example.env, and will start failing the
    moment a getenv appears without a line to match.
    """
    settings = set()
    for text in _repo_source().values():
        settings |= set(re.findall(r'getenv\(\s*"([A-Z][A-Z0-9_]*)"', text))

    assert settings, "the scan matched nothing"
    signing = sorted(s for s in settings
                     if "SIGN" in s or "COSIGN" in s or "POLICY" in s)
    assert not signing, (
        f"a signing setting exists ({signing}) but this test still says none "
        f"does -- replace it with one that pins the default"
    )
