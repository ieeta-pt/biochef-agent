from fastapi import FastAPI
import json
import oras.client
import os
import keyword
import shlex
import shutil
import stat

from dotenv import load_dotenv

from intermediate import IO, IOMode, Node, Param, Workflow

load_dotenv()

REGISTRY_URL = os.getenv("REGISTRY_URL", "registry.biochef.app")
REGISTRY_USERNAME = os.getenv("REGISTRY_USERNAME", " ")
REGISTRY_PASSWORD = os.getenv("REGISTRY_PASSWORD", " ")
REGISTRY_INSECURE = os.getenv("REGISTRY_INSECURE", "false").lower() == "true"
ORAS_AUTH_BACKEND = os.getenv("ORAS_AUTH_BACKEND", "token")

app = FastAPI()
client = oras.client.OrasClient(
    hostname=REGISTRY_URL,
    insecure=REGISTRY_INSECURE,
    auth_backend=ORAS_AUTH_BACKEND
)
client.login(
    username=REGISTRY_USERNAME,
    password=REGISTRY_PASSWORD
)

WRITE_INTERMEDIATE = os.getenv("BIOCHEF_WRITE_INTERMEDIATE", "false").lower() == "true"
"""Feature flag for the intermediate artifact (#2).

Off: the Snakefile is generated from the document held in memory, exactly as
before. On: the document is written to intermediate.json, read back, validated
against the schema, and the Snakefile is generated from that -- so the Snakefile
becomes a function of a file on disk rather than of the request body. The two
paths are asserted byte-identical over every operation in the catalogue by
tests/test_intermediate_roundtrip.py, which is what makes turning it on safe.
"""

INTERMEDIATE_FILENAME = "intermediate.json"


def intermediate_path(directory: str) -> str:
    """Where this run's document lives.

    A run has to say where. There is deliberately no default: a relative one
    would be resolved against whatever the process's working directory happened
    to be, and that is shared by every request in flight. While the handler
    chdir'd into a run directory that was invisibly fine; the moment it stops
    doing so -- which is what #40 is -- a default would silently put every
    concurrent run's document at the same path, and one run could then generate
    its Snakefile from another run's validated document.

    Removing the default is what makes that impossible to reintroduce by
    forgetting, rather than only fixed at the one call site that exists today.
    """
    return os.path.join(directory, INTERMEDIATE_FILENAME)


def write_intermediate(workflow: Workflow, directory: str) -> str:
    path = intermediate_path(directory)
    with open(path, "w") as f:
        f.write(workflow.to_json())
    return path


def read_intermediate(directory: str) -> Workflow:
    """Read the document back, validating it on the way in.

    A document that does not match the schema raises here rather than producing
    a nonsense Snakefile further downstream.
    """
    with open(intermediate_path(directory)) as f:
        return Workflow.from_json(f.read())


def through_intermediate(workflow: Workflow, directory: str) -> Workflow:
    """Round trip the document through this run's directory when the flag is on.

    `directory` is required for the reason given on intermediate_path.
    """
    if not WRITE_INTERMEDIATE:
        return workflow
    write_intermediate(workflow, directory)
    return read_intermediate(directory)


tools = {}
def fetch_tool(tool_id, repo):
    tool_id = tool_id.split("-")[0]

    if tool_id in tools:
        return tools[tool_id]

    client.pull(target=f"{REGISTRY_URL}/{repo}", outdir=f"{tool_id}")
    with open(f"{tool_id}/bundle.json", "r") as f:
        bundle = json.load(f)

    tool_bin = bundle["bin"]
    shutil.copyfile(f"{tool_id}/{tool_bin}", tool_bin)
    os.chmod(tool_bin, os.stat(tool_bin).st_mode |
             stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    tools[tool_id] = bundle
    return bundle


def get_node_data(node_id, node_list):
    return next(node for node in node_list if node["id"] == node_id)


def parse_biochef_workflow(biochef_workflow):
    node_list, edge_list = biochef_workflow["nodes"], biochef_workflow["edges"]
    new_workflow: Workflow = Workflow()

    for node in node_list:
        node_id, node_type = node["id"], node["type"]
        if node_type != "workflowNode":
            continue

        tool_info = fetch_tool(node_id, node["data"]["repo"])

        new_node: Node = Node(id=node_id, bin=tool_info["bin"])

        connections = [e for e in edge_list if node_id in (e["target"], e["source"])]
        for connection in connections:
            source, source_handle, target, target_handle = (
                connection["source"], connection.get("sourceHandle"),
                connection["target"], connection.get("targetHandle"),
            )

            _name = f"{source}-{source_handle}"

            def build_io(info):
                # Named rather than positional. `.get` returning None where a
                # recipe omits the key is kept as None rather than flattened to
                # "": the two mean different things to the frontend and both
                # occur in the catalogue, so the model records which one the
                # recipe actually said. Nothing downstream can tell the
                # difference -- the emitter only tests these for truth.
                return IO(
                    file=_name,
                    mode=IOMode(info.get("mode")),
                    hardcoded_file=info.get("filename"),
                    flag=info.get("flag"),
                )

            is_input_connection = node_id == target
            if is_input_connection:
                input_info = next(
                    i for i in tool_info["io"]["inputs"] if i["name"] == target_handle)
                new_node.inputs[_name] = build_io(input_info)
            else:
                output_info = next(
                    i for i in tool_info["io"]["outputs"] if i["name"] == source_handle)
                new_node.outputs[_name] = build_io(output_info)

        for param_key, param in node["data"]["paramValues"].items():
            if param.get("enabled") != True:
                continue
            param_info = next(
                p for p in tool_info["parameters"] if p["name"] == param_key)

            new_param: Param = Param(
                name=param_key,
                value=param["value"],
                flag=param_info.get("flag"),
            )

            new_node.parameters[param_key] = new_param

        new_workflow.nodes.append(new_node)

    return new_workflow


def sh_literal(value) -> str:
    """A token that must reach the shell as one argument, exactly as written.

    Two escapes, applied in this order, for two different readers.

    `shlex.quote` makes the value a single shell word, so a value of `; id`
    becomes `'; id'` and the shell reads text where it used to read a second
    command. `str()` first because a value is whatever JSON decoded -- a recipe
    default of `2` arrives as an int, and joining that raised TypeError (#37).

    Then the braces are doubled, because Snakemake formats the shell block
    before any shell sees it. Verified against snakemake 9.21.0: a lone `{` in a
    shell block is a NameError and the run exits 1, while `{{` renders as `{`.
    Doubling also means a value containing `{input.i_0}` is written out as text
    instead of expanding to another rule's field.
    """
    return shlex.quote(str(value)).replace("{", "{{").replace("}", "}}")


def sh_field(reference: str) -> str:
    """A Snakemake field reference, quoted by Snakemake when it expands.

    `:q` applies shell quoting to whatever the field expands to, so a path
    containing a space stays one argument rather than becoming two. Verified
    against snakemake 9.21.0.

    The braces here are deliberately NOT doubled -- they are what makes this a
    reference rather than literal text.
    """
    return f"{{{reference}:q}}"


def py_string(value: str) -> str:
    """A Python string literal for the input:/output: sections of a rule.

    Those sections are Python, not shell, so a name containing a quote would
    end the literal and let the rest be read as code. json.dumps produces the
    same bytes as the hand-written f-string for every name in the catalogue and
    stays valid for names that are not.
    """
    return json.dumps(value)


def rule_name_for(node_id: str) -> str:
    """Snakemake rule names are Python identifiers.

    A node id is "{operation.id}-{timestamp}", so both separators have to go.
    The result is checked rather than assumed: a node id is client-supplied, and
    one containing a newline would otherwise write new lines into the Snakefile
    at the point where a rule is declared -- which no amount of shell quoting
    further down would help with.
    """
    name = node_id.replace(".", "_").replace("-", "_")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"node id {node_id!r} does not make a usable rule name")
    return name


def convert_to_snakemake(workflow: Workflow):
    result = []
    result.append("rule all:\n    input:")

    for node in workflow.nodes:
        for output in node.outputs.values():
            result.append(f"        {py_string(output.file)},")

    for node in workflow.nodes:
        result.append(f"rule {rule_name_for(node.id)}:")
        cmd = [sh_literal(f"./{node.bin}")]
        extra_cms = []

        for param_name, param in node.parameters.items():
            if param.flag:
                cmd.append(sh_literal(param.flag))
            # An empty value has to stay absent rather than become an empty
            # argument. It contributed nothing before by accident: " ".join put
            # "" between two spaces and the shell collapsed the gap away. But
            # shlex.quote("") is '', which is a real argument -- and 100 of the
            # 176 catalogue operations declare flag-type parameters whose value
            # is empty, so quoting them turned "-c" into "-c ''" and handed a
            # tool an argument it never used to receive. Measured, not guessed:
            # the catalogue sweep for this change caught it.
            #
            # Emitting only the flag is also what the frontend does for these,
            # but making that the rule needs the parameter's declared type,
            # which the model does not carry yet. Skipping the empty value
            # reproduces today's behaviour exactly and keeps this change about
            # quoting and nothing else.
            if param.value != "":
                cmd.append(sh_literal(param.value))

        # Arguments with no flag are held back and appended after every flagged
        # one. A bare filename ahead of a flag makes a getopt-style parser stop
        # scanning, so the flags that follow are never seen: tn93 given
        # "in.fa -o out.txt" prints its usage and exits 1, while
        # "-o out.txt in.fa" runs. The frontend orders them the same way.
        trailing = []
        # Redirections are kept apart from the arguments and written last. The
        # shell accepts them anywhere, but "tool > out.txt in.fa" reads as
        # though the input were part of the redirect.
        redirects = []

        result.append("    input:")
        i = 0
        for input_name, input in node.inputs.items():
            input_var = f"i_{i}"
            result.append(f"        {input_var}={py_string(input.file)},")
            if input.mode == IOMode.STDIN:
                redirects.append(f"< {sh_field(f'input.{input_var}')}")
            elif input.mode == IOMode.FILE:
                if input.flag:
                    cmd.append(sh_literal(input.flag))
                    cmd.append(sh_field(f"input.{input_var}"))
                else:
                    trailing.append(sh_field(f"input.{input_var}"))
            i += 1

        result.append("    output:")
        i = 0
        for output_name, output in node.outputs.items():
            output_var = f"o_{i}"
            result.append(f"        {output_var}={py_string(output.file)},")
            if output.mode == IOMode.STDOUT:
                redirects.append(f"> {sh_field(f'output.{output_var}')}")
            elif output.mode == IOMode.FILE:
                if output.flag:
                    cmd.append(sh_literal(output.flag))
                    cmd.append(sh_field(f"output.{output_var}"))
                else:
                    trailing.append(sh_field(f"output.{output_var}"))
                if output.hardcoded_file and not output.flag:
                    extra_cms.append(
                        f"cp {sh_literal(output.hardcoded_file)} "
                        f"{sh_field(f'output.{output_var}')}"
                    )
            i += 1

        cmd.extend(trailing)
        cmd.extend(redirects)

        # Assembled outside the f-string. Nesting the same quote inside one is
        # only legal from Python 3.12 (PEP 701), and relying on that here made
        # the whole module fail to import on 3.11 and earlier -- for a line that
        # reads no better either way.
        command_line = " ".join(cmd)

        # A Python string literal, not a triple-quoted block.
        #
        # There is one more reader than the shell and Snakemake's formatter, and
        # it is the outermost one: a Snakefile IS Python source, so Python parses
        # this literal before anything else sees it. In a """...""" block that
        # meant two escapes:
        #
        #   a value containing a backslash escape was decoded by Python AFTER
        #   shlex.quote had finished, so "x\x27; touch PWNED; \x27" became a real
        #   quote and broke out of the shell quoting;
        #
        #   a value containing three double quotes ended the literal, and the
        #   rest of it ran as Python at parse time -- arbitrary code on the agent
        #   host, before any tool started, with the run still reporting success.
        #
        # json.dumps emits a literal in which neither is expressible: every
        # backslash and quote it produces is already escaped for Python, and the
        # value it decodes to is exactly the text that was assembled. The brace
        # doubling done by sh_literal survives untouched, so Snakemake's
        # formatter still reads what it should.
        shell_body = "\n".join([command_line] + extra_cms)
        result.append("    shell:")
        result.append(f"        {py_string(shell_body)}")

    return "\n".join(result)

# with open("test.json") as file:
#     workflow = parse_biochef_workflow(json.loads(file.read()))
#     print(workflow)
#     print(convert_to_snakemake(workflow))
