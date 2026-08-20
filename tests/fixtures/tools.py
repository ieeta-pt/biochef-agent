"""Tool bundles as the registry serves them, kept here so the converter can be
exercised without a registry.

Each entry is the operation block from a recipe, which is what bundle.json
contains alongside its runtime section."""

BUNDLES = {
    # positional input, output written via a flag
    "tn93.distance": {
        "id": "tn93.distance",
        "name": "tn93",
        "bin": "tn93",
        "io": {
            "inputs": [{"name": "in", "types": ["FASTA"], "mode": "file"}],
            "outputs": [{"name": "out", "types": ["TEXT"], "mode": "file", "flag": "-o"}],
        },
        "parameters": [],
    },
    # two positional inputs, output on stdout
    "edlib.align": {
        "id": "edlib.align",
        "name": "edlib",
        "bin": "edlib-aligner",
        "io": {
            "inputs": [
                {"name": "queries", "types": ["FASTA"], "mode": "file"},
                {"name": "target", "types": ["FASTA"], "mode": "file"},
            ],
            "outputs": [{"name": "out", "types": ["TEXT"], "mode": "stdout"}],
        },
        "parameters": [{"name": "mode", "type": "string", "flag": "-m"}],
    },
}
