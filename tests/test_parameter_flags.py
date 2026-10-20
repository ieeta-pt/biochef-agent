"""How a parameter reaches the command line.

lastz takes its options as --format=general and rejects --format= followed by
general as a separate word. The recipe declares the flag as "--format=", and
the browser joins the value to it; the agent must build the same command, or a
workflow that runs in the browser fails here.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

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

import convert


def _command_for(**parameters):
    node = convert.Node(id="tool-1", bin="t", parameters=parameters)
    snakefile = convert.convert_to_snakemake(convert.Workflow(nodes=[node]))
    return next(line.strip() for line in snakefile.splitlines()
                if line.strip().startswith("./t"))


def test_a_flag_ending_in_equals_takes_its_value_in_the_same_argument():
    command = _command_for(format=convert.Param("format", "general", "--format="))

    assert command == "./t --format=general"


def test_any_other_flag_takes_its_value_as_the_next_argument():
    command = _command_for(preset=convert.Param("preset", "asm5", "-x"))

    assert command == "./t -x asm5"


def test_a_parameter_without_a_flag_is_passed_as_is():
    command = _command_for(region=convert.Param("region", "chr1", ""))

    assert command == "./t chr1"
