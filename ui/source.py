"""The workflow as received and the Snakefile it would become, without running it.

The page has no network, so tools come from this machine's cache or not at all.
Nothing in the cache proves it is the bundle a step pins, because that takes the
registry's manifest, so the page says this over the Snakefile.
"""

import json

import convert


def workflow_json(raw):
    """The document exactly as received."""
    return raw.decode("utf-8", errors="replace")


def snakefile(raw):
    """(text, None), or (None, why it could not be generated)."""
    try:
        document = json.loads(raw)
        workflow = convert.parse_biochef_workflow(document,
                                                  resolve=convert.cached_tool)
        return convert.convert_to_snakemake(workflow), None
    except convert.ToolNotCached as missing:
        return None, f"The bundle of {missing} is not in this node's tool cache."
    except (convert.WorkflowInputError, ValueError, KeyError, TypeError,
            AttributeError, StopIteration) as failure:
        return None, (f"This workflow cannot be converted: "
                      f"{type(failure).__name__}: {failure}")
