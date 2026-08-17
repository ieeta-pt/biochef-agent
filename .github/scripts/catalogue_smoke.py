"""Drive every operation in the catalogue through the converter.

Compared against a checked-in baseline of operations that are known not to
convert, rather than requiring zero failures. Two reasons: some failures are
tracked in issues and fixing them is separate work, and a baseline makes the
diff of a PR that fixes one say exactly which one.

The check is two-sided on purpose. A new failure is a regression. A failure
that has been fixed but left in the baseline is also an error, because a stale
baseline quietly lowers the bar every time it is not updated.

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

    failures = {}
    for operation_id, operation in sorted(catalogue.items()):
        convert.tools.clear()
        convert.fetch_tool = lambda tool_id, repo, _o=operation: _o
        try:
            convert.convert_to_snakemake(
                convert.parse_biochef_workflow(editor_export(operation)))
        except Exception as error:
            failures[operation_id] = f"{type(error).__name__}: {error}"

    print(f"operations: {len(catalogue)}  converted: {len(catalogue) - len(failures)}"
          f"  failed: {len(failures)}")

    baseline = {}
    if os.path.exists(baseline_path):
        baseline = json.load(open(baseline_path)).get("known_failures", {})

    new = sorted(set(failures) - set(baseline))
    fixed = sorted(set(baseline) - set(failures))

    for operation_id in new:
        print(f"  NEW FAILURE  {operation_id}: {failures[operation_id]}")
    for operation_id in fixed:
        print(f"  NOW PASSES   {operation_id} -- remove it from the baseline")

    if new:
        sys.exit(f"{len(new)} operation(s) stopped converting")
    if fixed:
        sys.exit(f"{len(fixed)} operation(s) now convert; the baseline is stale")

    print("catalogue matches the baseline")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
