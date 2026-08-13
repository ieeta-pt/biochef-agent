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
    # Several arguments, so that their ORDER is observable. Every other bundle
    # here has at most one of each, which is why a round trip that reordered
    # them went unnoticed until the whole catalogue was swept -- see
    # test_argument_order_survives_the_round_trip.
    "multi.args": {
        "id": "multi.args",
        "name": "multi",
        "bin": "multi",
        "io": {
            "inputs": [
                {"name": "alpha", "types": ["TEXT"], "mode": "file", "flag": "-a"},
                {"name": "zulu", "types": ["TEXT"], "mode": "file"},
            ],
            "outputs": [{"name": "out", "types": ["TEXT"], "mode": "file", "flag": "-o"}],
        },
        # Deliberately not in alphabetical order: sorting anywhere in the
        # pipeline would rearrange these into z, m, a and change the command.
        "parameters": [
            {"name": "zeta", "type": "string", "flag": "-z"},
            {"name": "mu", "type": "string", "flag": "-u"},
            {"name": "alpha_p", "type": "string", "flag": "-p"},
        ],
    },
}
