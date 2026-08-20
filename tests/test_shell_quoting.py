"""#35, #42 and #37: every token that reaches the shell is quoted for what it is.

The emitter builds a command out of three kinds of token that look alike as
strings and must be escaped differently:

  literal      ./tn93, -m, a parameter value          shell-quote, then double braces
  field        {input.i_0}                            :q, and do NOT double the braces
  redirect     < and >                                neither

A single shlex.quote over the assembled line cannot work -- it would quote the
redirect operator too, and the tool would be handed a ">" as an argument.

The unit tests below pin the escaping. The snakemake test is the one that
matters: it asserts on the argv a real tool actually received, which is the only
statement anyone cares about. Reading the emitted string and judging it "looks
quoted" is exactly how this class of bug survives review.
"""

import ast
import json
import os
import shutil
import subprocess

import pytest

import convert
from tests.fixtures.tools import BUNDLES


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES[tool_id.split("-")[0]]
    )


# Payloads from #35, plus the ones the issue does not list.
PAYLOADS = [
    "NW",                          # ordinary, must survive untouched
    "a b",                         # would split into two arguments
    "; touch PWNED_SEMI",          # statement separator
    "$(touch PWNED_SUBST)",        # command substitution
    "`touch PWNED_BQ`",            # the older spelling
    "&& touch PWNED_AND",          # conditional
    "x\ntouch PWNED_NEWLINE",      # a newline, which no amount of escaping the
                                   # shell metacharacters would catch
    "' ; touch PWNED_QUOTE ; '",   # closes the quote the escaping adds
    "{input.i_0}",                 # reads another rule's field if left undoubled
    # The two that a shell-only model misses entirely, because the reader that
    # decodes them is Python, and it runs before the shell exists.
    r"x\x27; touch PWNED_HEXESC; \x27",   # Python turns \x27 into a real quote
    'a""" + str(__import__("os").system("touch PWNED_PYCODE")) + """b',
    "back\\slash",                 # survives both readers or is mangled by one
    "café",                        # non-ASCII, which json.dumps escapes as \\uXXXX
]


def workflow_with(value, tool="edlib.align", in_handle="queries", out_handle="out"):
    return {
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": f"{tool}-1", "type": "workflowNode",
             "data": {"label": "t", "repo": "r", "outputs": {},
                      "paramValues": {"mode": {"enabled": True, "value": value}}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": f"{tool}-1", "targetHandle": in_handle},
            {"source": f"{tool}-1", "sourceHandle": out_handle,
             "target": "output-1", "targetHandle": "in"},
        ],
    }


def shell_block(text):
    """The command the shell will actually receive.

    The body is emitted as a Python string literal, so recovering it means
    decoding that literal -- which is also the point of the test. json.loads
    rather than ast.literal_eval, so this asserts the literal really is
    JSON-shaped and reads it back through a different parser than wrote it.

    Anchored on the "shell:" line rather than on a leading quote: the input:
    and output: sections are quoted too, and matching those instead is a
    plausible way to write a test that passes while checking nothing.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "shell:":
            return json.loads(lines[i + 1].strip())
    raise AssertionError(f"no shell body found in:\n{text}")


# The command is a single literal now, so there is no separate notion of a
# "line" within it.
shell_line = shell_block


# --------------------------------------------------------------------------
# the escaping itself


def test_a_literal_is_one_shell_word():
    assert convert.sh_literal("a b") == "'a b'"
    assert convert.sh_literal("; id") == "'; id'"
    assert convert.sh_literal("NW") == "NW", "an ordinary value must not gain quotes"


def test_a_literal_has_its_braces_doubled():
    """Snakemake formats the block before any shell sees it.

    Verified against snakemake 9.21.0: a lone brace is a NameError and the run
    exits 1, so this is not only about stopping a field reference from being
    forged -- an unescaped brace breaks the run outright.
    """
    assert convert.sh_literal("{input.i_0}") == "'{{input.i_0}}'"
    assert convert.sh_literal("a{b}c") == "'a{{b}}c'"


def test_a_value_that_is_not_a_string_is_rendered_as_one():
    """#37: a recipe default of 2 arrives as JSON 2 and used to raise TypeError."""
    assert convert.sh_literal(2) == "2"
    assert convert.sh_literal(1.5) == "1.5"
    assert convert.sh_literal(True) == "True"


def test_a_field_is_quoted_by_snakemake_and_keeps_its_braces():
    assert convert.sh_field("input.i_0") == "{input.i_0:q}"
    assert "{{" not in convert.sh_field("output.o_0")


def test_a_rule_name_that_is_not_an_identifier_is_refused():
    """A node id is client-supplied, and reaches the Snakefile where a rule is
    declared. Quoting further down the line would not help with a newline here."""
    assert convert.rule_name_for("tn93.distance-1") == "tn93_distance_1"
    for bad in ['x:\n  shell: "id"', "", "1abc", "class", "a b"]:
        with pytest.raises(ValueError):
            convert.rule_name_for(bad)


def test_a_filename_is_written_as_a_python_literal():
    """The input:/output: sections are Python, not shell."""
    assert convert.py_string("a-out") == '"a-out"', "ordinary names must not change"
    assert convert.py_string('a"b') == '"a\\"b"'


# --------------------------------------------------------------------------
# what it produces


@pytest.mark.parametrize("payload", PAYLOADS)
def test_no_payload_escapes_its_argument(payload):
    command = shell_block(convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with(payload))))

    # The whole command, exactly: the tool, its flag, the value as a single
    # escaped token, the input, the redirect. Nothing the payload added.
    assert command == (
        f"./edlib-aligner -m {convert.sh_literal(payload)} "
        f"{{input.i_0:q}} > {{output.o_0:q}}"
    )


def test_an_empty_value_stays_absent_rather_than_becoming_an_empty_argument():
    """The regression the catalogue sweep caught, and unit tests could not.

    An empty value used to vanish by accident: " ".join put "" between two
    spaces and the shell collapsed the gap. shlex.quote("") is '' -- a real
    argument. 100 of the 176 catalogue operations declare flag-type parameters
    with an empty value, so quoting naively turned "-c" into "-c ''" and passed
    every one of them an argument it never received before.
    """
    command = shell_block(convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with(""))))

    assert command == "./edlib-aligner -m {input.i_0:q} > {output.o_0:q}"
    assert "''" not in command


@pytest.mark.parametrize("payload", PAYLOADS)
def test_python_and_json_decode_the_body_identically(payload):
    """The reader a shell-only model forgets.

    Snakemake parses a rule's `shell:` argument as Python, so the body has a
    Python reader ahead of the shell. In a triple-quoted block that meant a
    value containing `\\x27` was turned into a real quote by Python *after*
    shlex.quote had finished, and a value containing three quotes ended the
    literal so the rest ran as code.

    The property that makes it safe is that the emitted literal decodes to
    exactly the text that was assembled, under the Python parser that will
    actually read it. Checked against ast.literal_eval rather than only
    json.loads, so this is Python's own answer and not the writer's.

    Note the whole Snakefile is deliberately not compiled: it is Snakemake's
    DSL, and `rule all:` is not valid Python. Only the body is.
    """
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with(payload)))

    lines = sm.splitlines()
    literal = lines[[l.strip() for l in lines].index("shell:") + 1].strip()

    assert ast.literal_eval(literal) == json.loads(literal)
    assert ast.literal_eval(literal) == (
        f"./edlib-aligner -m {convert.sh_literal(payload)} "
        f"{{input.i_0:q}} > {{output.o_0:q}}"
    )


def test_the_shell_body_is_one_python_string_literal():
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with('a""" and b')))

    assert '"""' not in sm, "a triple-quoted block is what made this exploitable"
    body = shell_block(sm)
    assert body == './edlib-aligner -m \'a""" and b\' {input.i_0:q} > {output.o_0:q}'


def test_registry_supplied_strings_are_quoted_too(monkeypatch):
    """#42. These come from bundle.json, and the client chooses which repo is
    pulled, so they are no more trustworthy than a parameter value."""
    hostile = {
        "id": "h", "name": "h", "bin": "h",
        "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file",
                           "flag": "-i; touch PWNED #"}],
               "outputs": [{"name": "out", "types": ["T"], "mode": "file",
                            "filename": "fixed.txt; touch PWNED"}]},
        "parameters": [{"name": "mode", "type": "string", "flag": "-p $(id)"}],
    }
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: hostile)
    convert.tools.clear()

    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with("v", tool="h", in_handle="in")))

    assert "'-p $(id)'" in sm
    assert "'-i; touch PWNED #'" in sm
    assert "cp 'fixed.txt; touch PWNED'" in sm
    # nothing dangerous is left outside a quote
    for line in sm.splitlines():
        stripped = line.strip()
        if stripped.startswith("./") or stripped.startswith("cp "):
            assert "$(" not in stripped.replace("'$(id)'", "").replace("'-p $(id)'", "")


# --------------------------------------------------------------------------
# the only statement that really counts


SNAKEMAKE = shutil.which("snakemake")

# edlib.align: its output is stdout, so the stub prints and the generated
# redirect makes the file. It is the only fixture that declares a parameter.
STUB = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
json.dump(sys.argv[1:], open(os.path.join(here, "argv.json"), "w"))
sys.stdout.write("ok")
"""


@pytest.mark.skipif(not SNAKEMAKE, reason="snakemake is not installed")
@pytest.mark.parametrize("payload", PAYLOADS)
def test_the_tool_receives_the_payload_as_one_argument(payload, tmp_path):
    """Run it. The tool records its own argv, and we assert on that.

    Two things are checked and both matter: that no payload executed (no canary
    file appears), and that the value arrived intact -- a fix that mangled the
    value into safety would pass the first check and be useless.
    """
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with(payload)))

    (tmp_path / "Snakefile").write_text(sm)
    stub = tmp_path / "edlib-aligner"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "input-1-out").write_text("input-content")

    result = subprocess.run(
        [SNAKEMAKE, "--cores", "1", "-s", str(tmp_path / "Snakefile"),
         "-d", str(tmp_path), "all"],
        capture_output=True, text=True)

    canaries = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("PWNED"))
    assert canaries == [], f"payload executed: {canaries}\n{result.stderr[-2000:]}"
    assert result.returncode == 0, result.stderr[-2000:]

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[argv.index("-m") + 1] == payload, "the value did not arrive intact"
