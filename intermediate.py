"""The Agent's intermediate model.

The converter used to hold the workflow in four plain dataclasses that carried
no version and could not be written to disk at all -- `IOMode` is an `Enum`, so
`json.dumps(asdict(workflow))` raised `TypeError`. This module replaces them
with Pydantic models of the same shape, so the workflow becomes a document that
can be validated, written, read back, and recognised as belonging to a
particular revision of the contract.

Field names are deliberately unchanged from the dataclasses. `main.py` reads
`workflow.nodes`, `node.id`, `node.outputs.items()` and `output.file` directly,
and the emitter reads the rest; keeping the names means this swap is invisible
to both.

`Optional[...]` rather than `... | None` so the module still parses on 3.9. The
supported floor is 3.11 (see run.sh), but the tests are run on whatever
interpreter is to hand and there is no reason to make that harder.
"""

from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.1.0"
"""Semantic version of the document shape, not of the Agent.

Bump the minor for a backwards-compatible addition (a new optional field), the
major for anything that would make an older document invalid or change how an
existing field is read.
"""


class IOMode(str, Enum):
    """How a tool receives an input or emits an output.

    Inherits `str` so a member serialises as its own value and a document round
    trips through JSON without a custom encoder -- the specific thing the
    dataclasses could not do.
    """

    STDIN = "stdin"
    STDOUT = "stdout"
    FILE = "file"


ParamValue = Union[bool, int, float, str]
"""What a parameter value may be.

Deliberately not narrowed to `str`. The editor sends a recipe default through
unchanged, so a numeric default arrives as JSON `2`, not `"2"` -- and the
emitter then raises `TypeError: sequence item N: expected str instance, int
found` for 37 of the 176 catalogue operations. Coercing here would hide that
bug rather than fix it, and would make this change something other than
behaviour-preserving. The model records what actually arrives; #37 fixes the
emitter.

`bool` precedes `int` because `bool` is a subclass of `int`.
"""


class IO(BaseModel):
    """One end of an edge, as the tool that owns it expects to see it."""

    model_config = ConfigDict(extra="forbid")

    file: str = ""
    """The path the connected tools agree on for this input or output."""

    mode: Optional[IOMode] = None
    """How the tool reads or writes it."""

    hardcoded_file: Optional[str] = None
    """Some tools always write to a fixed name; it is copied to `file` after."""

    flag: Optional[str] = None
    """Some tools take the path behind a flag rather than positionally.

    `None` and `""` are deliberately kept apart, and this is the one place the
    model is more precise than the code it replaces rather than merely equal to
    it. A recipe that omits `flag` and a recipe that sets `flag: ""` mean
    different things to the frontend, and both occur: 300 io entries in the
    catalogue omit the key, and one -- `samtools.markdup.outputs.out` -- sets it
    empty on purpose.

    The emitter only ever tests it for truth, so nothing downstream changes.
    But typing this as `str = ""` would have collapsed the two at parse time and
    published that collapse as a contract, which is exactly the distinction #34
    has to be able to make when it is settled.
    """


class Param(BaseModel):
    """A parameter the client enabled, with the value it chose."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    value: ParamValue = ""
    flag: Optional[str] = None
    """`None` when the recipe declares no flag, for the same reason as `IO.flag`."""

    type: Optional[str] = None
    """What the recipe says this parameter is: flag, string, integer or float.

    Carried because the emitter cannot otherwise tell a flag from a value. The
    frontend branches on it -- a `flag` parameter contributes only its flag,
    never a value -- and without it here the agent has nothing to branch on.

    `Optional` rather than required so a document written before this field
    existed still validates. A missing type means "not declared", which the
    emitter treats as an ordinary value.
    """


class Node(BaseModel):
    """One tool invocation: what it runs, what it consumes, what it produces."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    bin: str = ""
    inputs: Dict[str, IO] = Field(default_factory=dict)
    outputs: Dict[str, IO] = Field(default_factory=dict)
    parameters: Dict[str, Param] = Field(default_factory=dict)


class Workflow(BaseModel):
    """The whole document. This is what `intermediate.json` contains."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: str = SCHEMA_VERSION
    nodes: List[Node] = Field(default_factory=list)

    def to_json(self) -> str:
        """Serialise for `intermediate.json`.

        Emphatically NOT sorted. `inputs`, `outputs` and `parameters` are dicts
        whose insertion order is load-bearing: the emitter walks them in order
        to build the argument list, so sorting the keys silently reorders the
        command line. Writing this with sort_keys=True changed the Snakefile for
        85 of the 176 catalogue operations -- every one with more than a single
        argument -- while every single-argument test still passed.

        The file is deterministic anyway, because the order it preserves is the
        order the parse produced.
        """
        import json

        return json.dumps(self.model_dump(mode="json"), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Workflow":
        """Read `intermediate.json` back, validating it against this schema.

        Raises `pydantic.ValidationError` if the document does not match --
        which is the point of the exercise: an invalid document is rejected
        here rather than producing a nonsense Snakefile downstream.
        """
        import json

        return cls.model_validate(json.loads(text))
