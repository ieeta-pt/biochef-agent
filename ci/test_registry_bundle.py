"""Manual compatibility check for one pinned BioCHEF registry bundle.

This checks the two runners, not the Agent's ORAS fetch or evidence-verification path.
"""

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import ApptainerRunner, SubprocessRunner
from workspace import make_workspace


REGISTRY = "registry.biochef.app"
PACKAGE = "biochef-plugins-gto.fasta.extract"
MANIFEST_DIGEST = "sha256:93e370288eee959b888e9748f09e424df455bc3665c528abeb29d7412bcddfdf"
FASTA = b">example\nACGTACGTACGT\n"
EXPECTED = b"GTA"
SNAKEFILE = """
rule all:
    input:
        "result.txt"

rule extract:
    input:
        "input.fa"
    output:
        "result.txt"
    shell:
        "./gto_fasta_extract -i 2 -e 5 < {input} > {output}"
"""


def _get_verified(kind, digest):
    request = urllib.request.Request(
        f"https://{REGISTRY}/v2/{PACKAGE}/{kind}/{digest}",
        headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise ValueError(f"{kind} digest mismatch: expected {digest}, got {actual}")
    return data


def _layer(manifest, title):
    matches = [
        layer for layer in manifest["layers"]
        if layer.get("annotations", {}).get("org.opencontainers.image.title") == title
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one {title} layer, found {len(matches)}")
    return matches[0]


def _run(runner, root):
    ws = make_workspace(root)
    try:
        source = Path(root) / "gto_fasta_extract"
        ws.place_executable(str(source), "gto_fasta_extract")
        ws.write_bytes("input.fa", FASTA)
        ws.write_bytes("Snakefile", (runner.snakefile_preamble() + SNAKEFILE).encode())
        result = runner.run(ws, timeout_s=600)
        if result.returncode != 0:
            raise RuntimeError(f"{runner.name} exited {result.returncode}: {result.stderr[-2000:]}")
        with ws.open_read("result.txt") as output:
            actual = output.read()
        if actual != EXPECTED:
            raise ValueError(f"{runner.name} produced {actual!r}, expected {EXPECTED!r}")
        print(f"{runner.name}: registry bundle returned {actual.decode()}")
    finally:
        ws.cleanup()


def main():
    manifest = json.loads(_get_verified("manifests", MANIFEST_DIGEST))
    bundle_layer = _layer(manifest, "bundle.json")
    bundle = json.loads(_get_verified("blobs", bundle_layer["digest"]))
    if bundle["bin"] != "gto_fasta_extract":
        raise ValueError(f"unexpected native executable: {bundle['bin']}")
    native_layer = _layer(manifest, bundle["bin"])
    binary = _get_verified("blobs", native_layer["digest"])
    if not binary.startswith(b"\x7fELF"):
        raise ValueError("native executable is not ELF")

    with tempfile.TemporaryDirectory(prefix="biochef-real-bundle-") as root:
        source = Path(root) / bundle["bin"]
        source.write_bytes(binary)
        os.chmod(source, 0o700)
        _run(SubprocessRunner(), root)
        _run(ApptainerRunner(cache_dir=str(Path(root) / "apptainer-cache")), root)
    print(f"OK: {PACKAGE}@{MANIFEST_DIGEST} ran under both runners")


if __name__ == "__main__":
    main()
