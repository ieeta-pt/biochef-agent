"""What changed between two versions, as lines a person can read. Pure functions."""

from ui.format import short_digest


def rule_lines(rules):
    lines = {}
    for name, value in sorted(rules.items()):
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines[name] = f"{name}: {value}"
    return lines


def rule_diff(old, new):
    """(kind, text) lines, changes first. Kind is removed, added or same."""
    before, after = rule_lines(old), rule_lines(new)
    changed, same = [], []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            same.append(("same", after[name]))
            continue
        if name in before:
            changed.append(("removed", before[name]))
        if name in after:
            changed.append(("added", after[name]))
    return changed + same


def count_rule_changes(old, new):
    before, after = rule_lines(old), rule_lines(new)
    return sum(before.get(name) != after.get(name)
               for name in before.keys() | after.keys())


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _tool(step):
    return f'{step["repo"]}  {short_digest(step["digest"])}'


def step_tags(old_steps, new_steps):
    """What happened to each step of `new_steps`, by id."""
    before = {step["id"]: step for step in old_steps}
    tags = {}
    for step in new_steps:
        previous = before.get(step["id"])
        if previous is None:
            tags[step["id"]] = "new step"
        elif (previous["repo"], previous["digest"]) != (step["repo"], step["digest"]):
            tags[step["id"]] = "tool changed"
        elif previous.get("paramValues") != step.get("paramValues"):
            tags[step["id"]] = "parameters changed"
        else:
            tags[step["id"]] = "unchanged"
    return tags


def _values(step):
    return " ".join(f"{name} {value}"
                    for name, value in sorted((step.get("paramValues") or {}).items()))


def step_diff(old_steps, new_steps):
    """Lines for the tools and parameter values that differ, with a closing note."""
    before = {step["id"]: step for step in old_steps}
    after = {step["id"]: step for step in new_steps}
    lines, params_changed = [], False
    for step in old_steps:
        if step["id"] not in after:
            lines.append(("removed", f'{step["id"]}  {_tool(step)}'))
    for step in new_steps:
        previous = before.get(step["id"])
        if previous is None:
            lines.append(("added", f'{step["id"]}  {_tool(step)}'))
            continue
        if (previous["repo"], previous["digest"]) != (step["repo"], step["digest"]):
            lines.append(("removed", f'{step["id"]}  {_tool(previous)}'))
            lines.append(("added", f'{step["id"]}  {_tool(step)}'))
        if previous.get("paramValues") != step.get("paramValues"):
            params_changed = True
            lines.append(("removed", f'{step["id"]}  {_values(previous)}'))
            lines.append(("added", f'{step["id"]}  {_values(step)}'))
    if not params_changed:
        lines.append(("same", "parameter values unchanged"))
    return lines
