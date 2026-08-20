"""Drive every operation in the catalogue through the converter and snapshot it.

Records, per operation, either the error it raises or a hash of the Snakefile it
produces, and compares that against a checked-in baseline.

Hashing the OUTPUT rather than only catching exceptions is the whole point, and
an earlier version of this script got it wrong. Checking "did it throw" catches
nothing about a change in what is emitted -- and the two regressions this job
exists to catch are both of that kind: serialising the intermediate document
with sorted keys reordered arguments for 85 of 176 operations, and quoting the
empty string added an argument to 100 of them. Neither raises. Both were
verified to sail straight through the exceptions-only version.

The check is three-sided. A new failure is a regression; an operation whose
output changed is a regression unless the baseline says so; and an entry that is
stale -- listed as failing but now converting -- is an error too, because a
stale baseline quietly lowers the bar every time it is not updated.

A PR that deliberately changes emitted output updates the baseline, and the diff
then states exactly which operations changed and how many. That is the point: it
makes an output change a reviewable fact rather than an invisible one.

Usage: catalogue_smoke.py <path-to-biochef-recipes> <path-to-baseline.json>
"""

import glob
import json
import os
import sys
import types


def stub_registry():
    """convert.py builds an ORAS client and logs in at import.

    Inline rather than importing the test conftest, because this runs as a
    script in CI and must not depend on the test package's layout.
    """
    if "oras" in sys.modules:
        return
    oras = types.ModuleType("oras")
    client = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("the catalogue smoke test reached the registry")

    client.OrasClient = _Client
    oras.client = client
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client


def operations(recipes_root):
    import yaml

    found = {}
    pattern = os.path.join(recipes_root, "**", "biochef.yaml")
    for path in glob.glob(pattern, recursive=True):
        document = yaml.safe_load(open(path)) or {}
        for operation in document.get("operations") or []:
            found[operation["id"]] = operation
    return found


def editor_export(operation):
    """One input node, the tool, one output node -- the shape the editor sends."""
    io = operation.get("io") or {}
    inputs, outputs = io.get("inputs") or [], io.get("outputs") or []
    values = {
        parameter["name"]: {"enabled": True, "value": parameter.get("default", "")}
        for parameter in (operation.get("parameters") or [])
    }
    nodes = [
        {"id": "source", "type": "inputWorkflowNode",
         "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
        {"id": operation["id"] + "-1", "type": "workflowNode",
         "data": {"label": "t", "repo": "r", "paramValues": values, "outputs": {}}},
        {"id": "sink", "type": "outputWorkflowNode", "data": {}},
    ]
    edges = []
    if inputs:
        edges.append({"source": "source", "sourceHandle": "out",
                      "target": operation["id"] + "-1",
                      "targetHandle": inputs[0]["name"]})
    if outputs:
        edges.append({"source": operation["id"] + "-1",
                      "sourceHandle": outputs[0]["name"],
                      "target": "sink", "targetHandle": "in"})
    return {"nodes": nodes, "edges": edges}


def main(recipes_root, baseline_path):
    stub_registry()
    sys.path.insert(0, os.getcwd())
    import convert

    catalogue = operations(recipes_root)
    if not catalogue:
        sys.exit(f"no operations found under {recipes_root} -- wrong path?")

    import hashlib

    observed = {}
    for operation_id, operation in sorted(catalogue.items()):
        convert.tools.clear()
        convert.fetch_tool = lambda tool_id, repo, _o=operation: _o
        try:
            snakefile = convert.convert_to_snakemake(
                convert.parse_biochef_workflow(editor_export(operation)))
        except Exception as error:
            observed[operation_id] = f"error: {type(error).__name__}: {error}"
        else:
            digest = hashlib.sha256(snakefile.encode()).hexdigest()[:16]
            observed[operation_id] = f"sha256: {digest}"

    failed = sum(1 for v in observed.values() if v.startswith("error:"))
    print(f"operations: {len(catalogue)}  converted: {len(catalogue) - failed}"
          f"  failed: {failed}")

    baseline = {}
    if os.path.exists(baseline_path):
        baseline = json.load(open(baseline_path)).get("operations", {})

    if not baseline:
        print("no baseline: writing one would be the next step")
        for operation_id, value in sorted(observed.items()):
            print(f"  {operation_id}: {value}")
        sys.exit("no baseline to compare against")

    missing = sorted(set(baseline) - set(observed))
    added = sorted(set(observed) - set(baseline))
    changed = sorted(k for k in set(observed) & set(baseline)
                     if observed[k] != baseline[k])

    for operation_id in added:
        print(f"  NEW OPERATION  {operation_id}: {observed[operation_id]}"
              f" -- add it to the baseline")
    for operation_id in missing:
        print(f"  GONE           {operation_id} -- remove it from the baseline")
    for operation_id in changed:
        print(f"  CHANGED        {operation_id}")
        print(f"      was {baseline[operation_id]}")
        print(f"      now {observed[operation_id]}")

    if changed:
        sys.exit(f"{len(changed)} operation(s) convert differently than the baseline. "
                 f"If that is intended, regenerate the baseline and the diff will say so.")
    if added or missing:
        sys.exit(f"the baseline does not match the catalogue "
                 f"({len(added)} added, {len(missing)} gone)")

    print("catalogue matches the baseline")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
