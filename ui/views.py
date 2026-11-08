"""What each screen shows, read from the store and shaped for the templates.

Every statement on a page comes from a row here, or the page says it does not
know. Nothing is carried over from the mocks.
"""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from node import egress, review
from store import repo
from store.states import Action, MessageKind, SubjectType, Verdict, VersionState
from ui import compare, format as fmt

UNKNOWN_MEMBER = "unknown member"

PAGE_SIZE = 20

STATE_LABELS = {"pending": "Waiting in Inbox", "approved": "Approved",
                "refused": "Refused", "withdrawn": "Withdrawn",
                "superseded": "Superseded"}
STATE_PILLS = {"pending": "warn", "approved": "ok", "refused": "bad",
               "withdrawn": "mute", "superseded": "mute"}
CHECK_LABELS = {
    "bad_signature": "bad signature", "wrong_node": "wrong node",
    "study_not_approved": "study not approved",
    "workflow_not_approved": "workflow not approved", "expired": "expired",
    "replay": "replay", "cannot_decrypt": "cannot decrypt",
    "hash_mismatch": "hash mismatch", "visa_missing": "visa missing",
    "dataset_not_bound": "dataset not bound",
}


def version_title(session, version):
    return f"{repo.study_heading(session, version)} · v{version.version}"


def workflow_title(workflow):
    if workflow.name and workflow.version:
        return f"{workflow.name} · {workflow.version}"
    return fmt.short_digest(workflow.digest)


def _entry_label(entry):
    identifier = entry.get("ls_aai_id")
    if not identifier:
        return UNKNOWN_MEMBER
    return repo.member_label(SimpleNamespace(display_name=None, ls_aai_id=identifier))


def _labels(session, version):
    """How the page names each member id of a version."""
    keys = {row.member: row for row in repo.member_keys(session, version.id)}
    labels = {member: repo.member_label(row) for member, row in keys.items()}
    for entry in version.members:
        labels.setdefault(entry["member"], _entry_label(entry))
    return labels, keys


def _key_text(row):
    canonical = json.dumps(row.signing_key, sort_keys=True).encode()
    print_ = hashlib.sha256(canonical).hexdigest()
    return (f"signing es256:{print_[:4]}…{print_[-4:]} · "
            f"result {row.result_key[:12]}…")


# The sidebar


def sidebar(session, now):
    waiting = (len(repo.pending_study_versions(session))
               + len(repo.pending_workflow_versions(session)))
    refused = repo.count_refused_since(session, now - timedelta(hours=24))
    status = repo.node_status(session)
    polled = None
    if status is not None and status.last_poll_at is not None:
        polled = fmt.ago(status.last_poll_at, now)
    return {"counts": {"Inbox": str(waiting) if waiting else None,
                       "Requests": f"{refused} refused" if refused else None},
            "polled": polled}


# Inbox


def _study_item(session, version, selected):
    replaced = repo.approved_version(session, version.study_id)
    keys = repo.member_keys(session, version.id)
    listed = {entry["member"] for entry in version.members}
    verified = {row.member for row in keys if row.identity_verified}
    failed = not listed or bool(listed - verified)
    if replaced is None:
        summary = (f'New study. {compare.plural(len(listed), "member")}, '
                   f'{compare.plural(len(version.nodes), "node")}.')
        pill = ("info", "New")
    else:
        before = {entry["member"] for entry in replaced.members}
        changes = []
        if listed - before:
            changes.append(f'{compare.plural(len(listed - before), "member")} added')
        if before - listed:
            changes.append(f'{compare.plural(len(before - listed), "member")} removed')
        rules = compare.count_rule_changes(replaced.output_rules, version.output_rules)
        if rules:
            changes.append(f'{compare.plural(rules, "output rule")} changed')
        detail = f' {", ".join(changes)}.' if changes else ""
        summary = f"Replaces approved v{replaced.version}.{detail}"
        pill = ("warn", "Re-approval needed")
    if failed:
        pill = ("bad", "Check failed · cannot be approved")
    return {"kind": "Study version", "at": version.received_at,
            "when": fmt.minute(version.received_at),
            "title": version_title(session, version), "summary": summary,
            "pill": pill, "study_id": version.id, "workflow_id": None,
            "selected": version.id == selected,
            "href": f"/ui/inbox/study/{version.id}"}


def _workflow_item(session, workflow):
    study_version = repo.get_study_version(session, workflow.study_version_id)
    summary = f"Under {version_title(session, study_version)}."
    base = baseline(session, workflow)
    if base is not None:
        tags = compare.step_tags(base.steps, workflow.steps)
        changed = sum(tag == "tool changed" for tag in tags.values())
        if changed:
            summary += f' {compare.plural(changed, "tool")} changed.'
    return {"kind": "Workflow version", "at": workflow.received_at,
            "when": fmt.minute(workflow.received_at),
            "title": workflow_title(workflow), "summary": summary,
            "pill": ("info", "New workflow version"), "study_id": None,
            "workflow_id": workflow.id, "selected": False,
            "href": f"/ui/workflows/{workflow.id}"}


def inbox_items(session, selected=None):
    """Everything waiting, newest first."""
    items = [_study_item(session, row, selected)
             for row in repo.pending_study_versions(session)]
    items += [_workflow_item(session, row)
              for row in repo.pending_workflow_versions(session)]
    return sorted(items, key=lambda item: item["at"], reverse=True)


def first_study(items):
    return next((item["study_id"] for item in items if item["study_id"]), None)


def _members(session, version, approved):
    labels, keys = _labels(session, version)
    before = {entry["member"] for entry in approved.members} if approved else None
    rows = []
    for entry in version.members:
        member = entry["member"]
        key = keys.get(member)
        rows.append({
            "label": labels[member], "proposer": member == version.proposer,
            "new": before is not None and member not in before,
            "verified": bool(key and key.identity_verified),
            "error": (key.identity_error if key else "No keys were received."),
            "key_text": _key_text(key) if key else None})
    removed = []
    if approved is not None:
        old_labels, _ = _labels(session, approved)
        now_listed = {entry["member"] for entry in version.members}
        removed = [old_labels[entry["member"]] for entry in approved.members
                   if entry["member"] not in now_listed]
    return rows, removed


def _nodes(session, version, settings):
    names = [entry.get("node", "") for entry in version.nodes]
    latest = repo.latest_federation_list(session)
    if latest is None:
        check = ("warn", "No signed node list yet")
    else:
        known = {entry.get("node") for entry in latest.nodes}
        missing = [name for name in names if name not in known]
        if missing:
            check = ("bad", f'Not on the signed node list v{latest.version}: '
                            f'{", ".join(missing)}')
        else:
            check = ("ok", f"all on the signed node list v{latest.version}")
    shown = [f"{name} (this node)" if name == settings.node_name else name
             for name in names]
    return {"names": ", ".join(shown), "check": check}


def _datasets(session, version):
    return [{"id": dataset,
             "bound": repo.dataset_binding(session, dataset) is not None}
            for dataset in version.datasets]


def rules_verdict(version, policy):
    if policy is None:
        return None
    verdict = policy.check_rules(version.output_rules)
    return {"name": policy.name, "acceptable": verdict.acceptable,
            "text": verdict.text}


def _approval_blockers(session, version, approved_row, failed):
    blockers = [f"{row['label']}: identity not verified" for row in failed]
    if not version.members:
        blockers.append("This version lists no members.")
    if approved_row is not None and approved_row.version > version.version:
        blockers.append(f"v{approved_row.version} is already approved, and an "
                        f"older version cannot replace it.")
    return blockers


def study_detail(session, version, settings, policy):
    approved = None
    pending = version.state == VersionState.PENDING.value
    current = repo.approved_version(session, version.study_id)
    if pending and current is not None and current.id != version.id:
        approved = current
    members, removed = _members(session, version, approved)
    failed = [row for row in members if not row["verified"]]
    labels, _ = _labels(session, version)

    compared = None
    if approved is not None:
        decision = _last_action(session, approved, Action.APPROVE)
        when = fmt.day(decision.at) if decision else "date not recorded"
        compared = {"version": approved.version, "when": when,
                    "href": f"/ui/studies/{approved.study_id}"}

    if approved is not None:
        lines = compare.rule_diff(approved.output_rules, version.output_rules)
        rules_changed = compare.count_rule_changes(approved.output_rules,
                                                   version.output_rules) > 0
    else:
        lines = [("same", line)
                 for line in compare.rule_lines(version.output_rules).values()]
        rules_changed = False

    supersedes = (f"Approving v{version.version} supersedes v{approved.version}. "
                  if approved else "")
    return {
        "id": version.id, "version": version.version, "pending": pending,
        "state": version.state, "state_label": STATE_LABELS[version.state],
        "state_pill": STATE_PILLS[version.state],
        "title": version_title(session, version), "compared": compared,
        "proposer": labels.get(version.proposer, UNKNOWN_MEMBER),
        "members": members, "removed": removed, "failed": failed,
        "datasets": _datasets(session, version),
        "nodes": _nodes(session, version, settings),
        "recipients": "Requester only",
        "recipients_note": f"Unchanged from v{approved.version}" if approved else None,
        "rule_lines": lines, "rules_changed": rules_changed,
        "verdict": rules_verdict(version, policy) if pending else None,
        "blockers": _approval_blockers(session, version, current, failed),
        "approve_note": f"{supersedes}Only one version of a study is valid at a time.",
        "status": status_of(session, SubjectType.STUDY_VERSION, version),
    }


def _last_action(session, row, action, subject=SubjectType.STUDY_VERSION):
    for decision in repo.decisions_about(session, subject, row.id):
        if decision.action == action.value:
            return decision
    return None


# After a decision


def _study_message(session, version, decision):
    label = f"v{version.version}"
    action = Action(decision.action)
    if action is Action.APPROVE:
        replaced = repo.versions_replaced_at(session, version)
        if replaced is None:
            return "ok", f"{label} approved and signed."
        return "ok", (f"{label} approved and signed. v{replaced.version} was "
                      f"superseded at the same time; its runs and logs stay in the "
                      f"study history.")
    if action is Action.REFUSE:
        current = repo.approved_version(session, version.study_id)
        message = f"{label} refused and signed"
        message += ", with your comment." if decision.comment else "."
        if current is not None:
            message += f" v{current.version} stays approved."
        return "bad", message
    return "bad", f"{label} withdrawn and signed."


def _workflow_message(session, workflow, decision):
    action = Action(decision.action)
    if action is Action.APPROVE:
        study_version = repo.get_study_version(session, workflow.study_version_id)
        return "ok", (f"Approved and signed. {workflow_title(workflow)} is now "
                      f"listed under {version_title(session, study_version)}.")
    word = "Refused" if action is Action.REFUSE else "Withdrawn"
    return "bad", f"{word} and signed."


def status_of(session, subject_type, row):
    """The line shown while a decision has not left this node, or None."""
    about = repo.decisions_about(session, subject_type, row.id)
    if not about or about[0].posted_to_coordinator_at is not None:
        return None
    decision = about[0]
    if decision.reason == review.SUPERSEDED:
        return None
    if subject_type is SubjectType.STUDY_VERSION:
        kind, message = _study_message(session, row, decision)
    else:
        kind, message = _workflow_message(session, row, decision)
    return {"kind": kind, "message": message, "decision_id": decision.id,
            "can_undo": review.undo_refusal(session, decision) is None}


# Studies


def _main_version(versions):
    """The version a study is shown by: the approved one, else the latest decided."""
    for row in versions:
        if row.state == VersionState.APPROVED.value:
            return row
    for row in versions:
        if row.state != VersionState.PENDING.value:
            return row
    return None


def _study_card(session, study_id, selected):
    versions = repo.study_versions(session, study_id)
    main = _main_version(versions)
    if main is None or main.state == VersionState.REFUSED.value:
        return None
    listed = repo.workflow_versions(session, main.id)
    workflows = [row for row in listed if row.state == VersionState.APPROVED.value]
    unreviewed = [row for row in listed if row.state == VersionState.PENDING.value]
    requests = repo.count_requests_of_study(session, study_id)
    waiting = next((row for row in versions
                    if row.state == VersionState.PENDING.value), None)
    if main.state == VersionState.APPROVED.value:
        # The Workflows tab counts every version of the study, so a version still
        # waiting for review is said here too, or the two numbers would disagree.
        parts = [f'{compare.plural(len(workflows), "workflow")} approved']
        if unreviewed:
            parts.append(f"{len(unreviewed)} waiting")
        parts.append(compare.plural(requests, "request"))
        note = " · ".join(parts)
    else:
        note = (f"{STATE_LABELS[main.state]} {fmt.day(main.state_changed_at)}, "
                f"kept for the audit log")
    return {"study_id": study_id, "title": version_title(session, main),
            "state": STATE_LABELS[main.state], "pill": STATE_PILLS[main.state],
            "approved": main.state == VersionState.APPROVED.value, "note": note,
            "waiting": waiting and {"version": waiting.version,
                                    "href": f"/ui/inbox/study/{waiting.id}"},
            "selected": study_id == selected, "href": f"/ui/studies/{study_id}"}


def study_cards(session, selected=None):
    cards = [_study_card(session, study_id, selected)
             for study_id in repo.studies_with_versions(session)]
    cards = [card for card in cards if card is not None]
    return sorted(cards, key=lambda card: not card["approved"])


def first_card(cards):
    return cards[0]["study_id"] if cards else None


def _version_row(session, version, versions):
    href = None
    if version.state == VersionState.PENDING.value:
        href = f"/ui/inbox/study/{version.id}"
    about = repo.decisions_about(session, SubjectType.STUDY_VERSION, version.id)
    decision = about[0] if about else None
    by = "n/a"
    if decision is not None:
        by = decision.user
        if decision.reason == review.SUPERSEDED:
            newer = next(
                (row for row in reversed(versions)
                 if row.version > version.version and row.state_changed_at
                 == version.state_changed_at), None)
            by = (f"automatic, when v{newer.version} was approved by {decision.user}"
                  if newer else f"automatic, by {decision.user}")
    return {"version": version.version, "href": href,
            "state": STATE_LABELS[version.state], "pill": STATE_PILLS[version.state],
            "decided": fmt.minute(decision.at) if decision else "n/a", "by": by}


def study_page(session, study_id, settings, tab):
    versions = repo.study_versions(session, study_id)
    if not versions:
        return None
    main = _main_version(versions) or versions[0]
    labels, keys = _labels(session, main)
    approval = _last_action(session, main, Action.APPROVE)
    subtitle = STATE_LABELS[main.state]
    if main.state == VersionState.APPROVED.value and approval is not None:
        subtitle = f"Approved {fmt.day(approval.at)} by {approval.user}"
    elif main.state != VersionState.PENDING.value:
        subtitle = f"{subtitle} {fmt.day(main.state_changed_at)}"
    listed = [entry["member"] for entry in main.members]
    verified = all(keys.get(member) is not None and keys[member].identity_verified
                   for member in listed)
    return {
        "study_id": study_id, "version_id": main.id,
        "title": version_title(session, main), "subtitle": subtitle,
        "can_withdraw": main.state == VersionState.APPROVED.value,
        "tab": tab,
        "workflow_count": len(repo.workflow_versions(session, main.id)),
        "request_count": repo.count_requests_of_study(session, study_id),
        "status": status_of(session, SubjectType.STUDY_VERSION, main),
        "members": [{"label": labels[member],
                     "proposer": member == main.proposer} for member in listed],
        "verified": verified, "datasets": _datasets(session, main),
        "recipients": "Requester only", "required_visa": settings.required_visa,
        "rule_lines": list(compare.rule_lines(main.output_rules).values()),
        "versions": [_version_row(session, row, versions) for row in versions],
    }


def _param_summary(steps):
    pairs = []
    for step in steps:
        for name, value in (step.get("paramValues") or {}).items():
            pairs.append(f"{name} {value}")
    return " · ".join(pairs)


def workflow_rows(session, version_id):
    version = repo.get_study_version(session, version_id)
    rows = []
    for workflow in repo.workflow_versions(session, version_id):
        count = repo.count_requests_of_workflow(
            session, workflow.study_id, version.version, workflow.digest)
        pending = workflow.state == VersionState.PENDING.value
        rows.append({
            "id": workflow.id, "title": workflow_title(workflow),
            "params": _param_summary(workflow.steps),
            "digest": fmt.short_digest(workflow.digest).partition(":")[2],
            "state": "New version" if pending else STATE_LABELS[workflow.state],
            "pill": "info" if pending else STATE_PILLS[workflow.state],
            "requests": "n/a" if pending else str(count), "pending": pending,
            "can_withdraw": workflow.state == VersionState.APPROVED.value,
            "status": status_of(session, SubjectType.WORKFLOW_VERSION, workflow)})
    return rows


def _check_pill(request):
    if request.verdict == Verdict.REFUSED.value:
        label = CHECK_LABELS.get(request.failed_check, request.failed_check)
        return "bad", f"Refused · {label}"
    if request.verdict == Verdict.ACCEPTED.value:
        return "ok", "Accepted"
    return "mute", "Not checked yet"


def _run_text(session, request):
    if request.verdict != Verdict.ACCEPTED.value or request.run_id is None:
        return "n/a"
    run = repo.get_run(session, request.run_id)
    if run.state != "COMPLETE":
        return run.state.capitalize()
    if run.egress_state == "sent":
        return "Done · sent"
    if run.egress_state in ("released", "withheld"):
        return f"Done · {run.egress_state}"
    return "Done · in review"


def request_rows(session, study_id, offset=0):
    """A page of a study's requests, and how many remain after it."""
    versions = repo.study_versions(session, study_id)
    titles, labels = {}, {}
    for version in versions:
        version_labels, _ = _labels(session, version)
        labels[version.version] = version_labels
        for workflow in repo.workflow_versions(session, version.id):
            titles[workflow.digest] = workflow_title(workflow)
    rows = []
    for request in repo.requests_of_study(session, study_id, limit=PAGE_SIZE,
                                          offset=offset):
        rows.append({
            "request_id": request.request_id,
            "member": labels.get(request.study_version, {}).get(
                request.member, UNKNOWN_MEMBER),
            "workflow": titles.get(request.workflow_digest,
                                   fmt.short_digest(request.workflow_digest)),
            "check": _check_pill(request), "run": _run_text(session, request),
            "run_id": (request.run_id if request.verdict == Verdict.ACCEPTED.value
                       else None)})
    total = repo.count_requests_of_study(session, study_id)
    shown = offset + len(rows)
    return {"rows": rows, "older": max(0, total - shown), "next_offset": shown,
            "study_id": study_id}


# Requests

REQUEST_CHECKS = (
    ("bad_signature", "Request signed by a study member"),
    ("wrong_node", "Addressed to this node"),
    ("study_not_approved", "Study version approved here"),
    ("workflow_not_approved", "Workflow hash approved for this study"),
    ("expired", "Not expired"),
    ("replay", "Not seen before"),
    ("cannot_decrypt", "Payload decrypts with this node key"),
    ("hash_mismatch", "Workflow hash recomputed from tools and values"),
    ("visa_missing", "Member visas allow the datasets"),
    ("dataset_not_bound", "Datasets bound to local data"),
)


def _check_marks(request):
    """Each check as passed, failed or not reached, from what the request recorded."""
    if request.verdict is None:
        return [("waiting", text) for _, text in REQUEST_CHECKS]
    stop = None
    if request.verdict == Verdict.REFUSED.value:
        stop = request.failed_check
    marks, reached = [], True
    for code, text in REQUEST_CHECKS:
        if not reached:
            marks.append(("not_reached", text))
        elif code == stop:
            marks.append(("failed", text))
            reached = False
        else:
            marks.append(("passed", text))
    return marks


def _request_pill(session, request):
    if request.verdict == Verdict.ACCEPTED.value and request.run_id:
        run = repo.get_run(session, request.run_id)
        if run.egress_state == "sent":
            return "ok", "Accepted · result sent"
        return "ok", f"Accepted · run {run.state}"
    return _check_pill(request)


def _titles_of_study(session, request):
    version = repo.study_version_named(session, request.study_id,
                                       request.study_version)
    if version is None:
        return f"{request.study_id} · v{request.study_version}", {}, {}
    titles = {row.digest: workflow_title(row)
              for row in repo.workflow_versions(session, version.id)}
    labels, _ = _labels(session, version)
    return version_title(session, version), titles, labels


def _request_row(session, request):
    study, titles, labels = _titles_of_study(session, request)
    pill = _request_pill(session, request)
    return {"id": request.request_id, "study": study,
            "workflow": titles.get(request.workflow_digest,
                                   fmt.short_digest(request.workflow_digest)),
            "member": labels.get(request.member, UNKNOWN_MEMBER),
            "when": fmt.minute(request.received_at), "pill": pill}


def _repeats(request):
    """What the deliveries after the first got. None when there were none."""
    if not request.repeats:
        return None
    times = "once" if request.repeats == 1 else f"{request.repeats} times"
    got = ("joined this run" if request.verdict == Verdict.ACCEPTED.value
           else "got the same refusal")
    return (f"Delivered again {times}, last {fmt.minute(request.last_repeat_at)}. "
            f"The same request_id {got}.")


def _visa_reasons(session, request, required_visa):
    """Why a visa_missing refusal can never pass as things are: a value nobody set.

    This is the binding as it is now, which is what a new request would meet.
    """
    if request.failed_check != "visa_missing" or not required_visa:
        return []
    reasons = []
    for dataset in request.datasets:
        row = repo.dataset_binding(session, dataset)
        if row is not None and not row.visa_value:
            reasons.append(f"{dataset} has no visa value on this node, so no "
                           f"{required_visa} visa can match it. Set it on the "
                           f"Datasets page.")
    return reasons


def _request_panel(session, request, required_visa=None):
    has_run = request.verdict == Verdict.ACCEPTED.value and request.run_id is not None
    return {"id": request.request_id, "expires": fmt.minute(request.not_after),
            "marks": _check_marks(request), "run_id": request.run_id if has_run else None,
            "refused": request.verdict == Verdict.REFUSED.value,
            "repeats": _repeats(request),
            "reasons": _visa_reasons(session, request, required_visa)}


FILTERS = {"all": None, "accepted": (Verdict.ACCEPTED,),
           "refused": (Verdict.REFUSED,)}


def requests_page(session, now, shown="all", selected=None, required_visa=None):
    since = now - timedelta(hours=24)
    everything = repo.requests_since(session, since)
    counts = {"all": len(everything),
              "accepted": len(repo.requests_since(session, since, FILTERS["accepted"])),
              "refused": len(repo.requests_since(session, since, FILTERS["refused"]))}
    rows = repo.requests_since(session, since, FILTERS[shown])
    chosen = next((row for row in rows if row.request_id == selected),
                  rows[0] if rows else None)
    return {"filter": shown, "selected": chosen.request_id if chosen else None,
            "tabs": [("all", f'All {counts["all"]}'),
                     ("accepted", f'Accepted {counts["accepted"]}'),
                     ("refused", f'Refused {counts["refused"]}')],
            "rows": [_request_row(session, row) for row in rows],
            "panel": (_request_panel(session, chosen, required_visa) if chosen
                      else None)}


# Run

STEP_PILLS = {"COMPLETE": "ok", "RUNNING": "info", "FAILED": "bad", "PENDING": "mute"}
RUN_PILLS = {"COMPLETE": "ok", "RUNNING": "info", "QUEUED": "mute",
             "INITIALIZING": "info", "CANCELING": "warn", "CANCELED": "mute",
             "EXECUTOR_ERROR": "bad", "SYSTEM_ERROR": "bad"}
EGRESS_LABELS = {
    "not_ready": ("mute", "Egress not started"),
    "rules_pending": ("mute", "Checking output rules"),
    "awaiting_airlock": ("warn", "Waiting for the airlock"),
    "awaiting_review": ("warn", "Waiting for your review"),
    "released": ("ok", "Released"), "withheld": ("bad", "Withheld"),
    "sent": ("ok", "Result sent")}
LIVE_STATES = ("QUEUED", "INITIALIZING", "RUNNING", "CANCELING")


def _size(bytes_):
    if bytes_ >= 1_000_000:
        return f"{bytes_ / 1_000_000:.1f} MB"
    if bytes_ >= 1_000:
        return f"{bytes_ // 1_000} KB"
    return f"{bytes_} B"


def _duration(step):
    if not step.get("started_at") or not step.get("ended_at"):
        return None
    start = datetime.fromisoformat(step["started_at"])
    end = datetime.fromisoformat(step["ended_at"])
    return f"{max(1, round((end - start).total_seconds() / 60))} min"


def _egress_decision(session, run_id):
    about = repo.decisions_about(session, SubjectType.EGRESS, run_id)
    return about[0] if about else None


def run_page(session, run, tab, settings, log="run"):
    request = (repo.get_request(session, run.request_id)
               if run.request_id else None)
    study = workflow = member = None
    if request is not None:
        study, titles, labels = _titles_of_study(session, request)
        workflow = titles.get(request.workflow_digest,
                              fmt.short_digest(request.workflow_digest))
        member = labels.get(request.member, UNKNOWN_MEMBER)
    files = run.egress_files
    held = sum(row.get("verdict") == "held_back" for row in files)
    good = sum(row.get("verdict") == "pass" for row in files)
    decision = _egress_decision(session, run.run_id)
    notice = repo.message_of(session, run.run_id, MessageKind.WITHHELD_RESULT)
    can_undo = bool(decision and decision.posted_to_coordinator_at is None
                    and run.egress_state in ("released", "withheld"))
    return {
        "id": run.run_id, "tab": tab, "log": log,
        "request_id": run.request_id, "study": study, "workflow": workflow,
        "member": member,
        "started": fmt.minute(run.started_at) if run.started_at else None,
        "ended": fmt.minute(run.ended_at) if run.ended_at else None,
        "state": run.state, "state_pill": RUN_PILLS.get(run.state, "mute"),
        "egress": EGRESS_LABELS[run.egress_state], "egress_state": run.egress_state,
        "mode": settings.egress_mode,
        "steps": [{"id": row["id"], "state": row["state"],
                   "pill": STEP_PILLS.get(row["state"], "mute"),
                   "time": _duration(row)} for row in run.steps],
        "files": [{"file": row["file"], "size": _size(row["size"]),
                   "pass": row.get("verdict") == "pass",
                   "judged": row.get("verdict") is not None,
                   "reason": row.get("reason"),
                   "unverified": row.get("unverified", [])} for row in files],
        "good": good, "held": held,
        "unverified": egress.unverified(run),
        "airlock_at": fmt.minute(run.airlock_at) if run.airlock_at else None,
        "notice": notice and {"sent": fmt.minute(notice.sent_at) if notice.sent_at
                              else None},
        "review_open": (run.egress_state == "awaiting_review"
                        and settings.egress_mode == "manual"),
        "decision": decision and {
            "id": decision.id, "by": decision.user, "at": fmt.minute(decision.at),
            "action": decision.action, "can_undo": can_undo},
        "live": run.state in LIVE_STATES,
        "can_cancel": run.state in ("QUEUED", "INITIALIZING", "RUNNING")
        and not run.cancel_requested,
        "cancel_requested": run.cancel_requested,
        "log_tabs": ["run"] + [row["id"] for row in run.steps],
        "log_text": _log_text(run, log), "node_logs_ready": not run.state in LIVE_STATES,
        "has_manifest": run.manifest_path is not None,
        "exit_code": run.exit_code,
    }


def _log_text(run, log):
    if log == "run":
        return (run.log_stdout + run.log_stderr) or "Nothing logged yet."
    entry = run.node_logs.get(log)
    if entry is None:
        return "This step has no log. Per step logs exist only after the run ends."
    return (entry.get("stdout", "") + entry.get("stderr", "")) or "Nothing logged."


# Datasets


def _used_by(session, dataset_id, versions):
    names = []
    for version in versions:
        if dataset_id in version.datasets:
            title = version_title(session, version)
            pending = version.state == VersionState.PENDING.value
            names.append(f"{title} (in Inbox)" if pending else title)
    return ", ".join(names) or "no approved study"


def dataset_page(session):
    versions = repo.open_versions(session)
    bound = repo.dataset_bindings(session)
    rows = [{"id": row.dataset_id, "type": row.type, "location": row.location,
             "visa_value": row.visa_value,
             "used_by": _used_by(session, row.dataset_id, versions),
             # Only a dataset a pending or approved study names can be bound or
             # rebound from the page (node.datasets.bind), so only then is Edit offered.
             "named": any(row.dataset_id in version.datasets for version in versions),
             "locked": row.source == "config", "bound_by": row.bound_by}
            for row in bound]
    known = {row.dataset_id for row in bound}
    waiting, seen = [], set()
    for version in versions:
        for dataset_id in version.datasets:
            if dataset_id not in known and dataset_id not in seen:
                seen.add(dataset_id)
                state = (", in the Inbox"
                         if version.state == VersionState.PENDING.value else "")
                waiting.append({"id": dataset_id, "named_by":
                                f"{version_title(session, version)}{state}"})
    return {"rows": rows, "waiting": waiting}


# Workflow version


def _version_key(text):
    parts = []
    for part in (text or "").split("."):
        parts.append(int(part) if part.isdigit() else 0)
    return parts


def baseline(session, workflow):
    """The approved version of the same workflow to compare with, if it has a name."""
    if not workflow.name:
        return None
    candidates = [row for row in
                  repo.workflow_versions(session, workflow.study_version_id)
                  if row.id != workflow.id and row.name == workflow.name
                  and row.state == VersionState.APPROVED.value]
    if not candidates:
        return None

    def closeness(row):
        # Parameter variants share a version: compare with the one nearest this.
        return (_version_key(row.version),
                -len(compare.step_diff(row.steps, workflow.steps)))

    return max(candidates, key=closeness)


def _step_view(step, tag, index):
    return {"index": index, "id": step["id"], "repo": step["repo"],
            "digest": fmt.short_digest(step["digest"]),
            "reference": f'{step["repo"]}@{fmt.short_digest(step["digest"])}',
            "signature": step.get("signature"), "tag": tag,
            "description": step.get("description"),
            "params": sorted((step.get("paramValues") or {}).items())}


def workflow_page(session, workflow, step_index=None):
    version = repo.get_study_version(session, workflow.study_version_id)
    base = baseline(session, workflow)
    tags = compare.step_tags(base.steps, workflow.steps) if base else {}
    steps = [_step_view(step, tags.get(step["id"]), index)
             for index, step in enumerate(workflow.steps)]
    if step_index is None or not 0 <= step_index < len(steps):
        changed = [step for step in steps
                   if step["tag"] not in (None, "unchanged")]
        step_index = changed[0]["index"] if changed else 0
    pending = workflow.state == VersionState.PENDING.value
    blocker = None
    if pending and version.state != VersionState.APPROVED.value:
        blocker = (f"Its study version, v{version.version}, is "
                   f"{STATE_LABELS[version.state].lower()}. A workflow can be "
                   f"approved only under an approved study version.")
    title = workflow_title(workflow)
    base_title = workflow_title(base) if base else None
    base_version = base.version if base else None
    return {
        "id": workflow.id, "title": title, "pending": pending,
        "state": STATE_LABELS[workflow.state], "pill": STATE_PILLS[workflow.state],
        "kicker": ("Workflow version · waiting for this node" if pending
                   else f"Workflow version · {STATE_LABELS[workflow.state].lower()}"),
        "digest": fmt.short_digest(workflow.digest),
        "study_title": version_title(session, version),
        "study_href": f"/ui/studies/{workflow.study_id}",
        "base_title": base_title, "base_version": base_version,
        "steps": steps, "selected": steps[step_index] if steps else None,
        "datasets": version.datasets,
        "changes": compare.step_diff(base.steps, workflow.steps) if base else None,
        "blocker": blocker, "approve_label": f"Approve {workflow.version or title} and sign",
        "approve_note": (
            f"{base_version} stays approved. Requests name the hash they want, so "
            f"each runs only the version it asked for." if base else None),
        "status": status_of(session, SubjectType.WORKFLOW_VERSION, workflow),
    }
