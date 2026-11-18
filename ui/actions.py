"""Running a decision from a page.

A decision answers with a redirect, because the page it lands on already knows
how to show a pending decision and its Undo from the store. That way a reload, or
coming back later, shows the same thing. A refusal comes back as an alert in place.
"""

from fastapi import Request
from fastapi.responses import Response

import audit
from node.datasets import BindingError
from node.decisions import DecisionError
from store import db
from store.errors import (AlreadyPosted, IdentityNotVerified, IllegalTransition,
                          StoreError)
from ui.render import fragment

MAX_COMMENT = 2000


def bind_of(request: Request):
    return request.app.state.bind


def clean_comment(comment):
    return (comment or "").strip()[:MAX_COMMENT] or None


def _explain(failure):
    if isinstance(failure, IdentityNotVerified):
        return ("This version cannot be approved: a member's identity is not "
                "verified.")
    if isinstance(failure, IllegalTransition):
        return "This was already decided. Reload the page to see where it stands."
    if isinstance(failure, AlreadyPosted):
        return "This decision has already been sent, so it cannot be undone."
    return str(failure)


def act(request, work, *, then=None):
    """Run `work(session)` in one transaction. Redirects, or refreshes without `then`."""
    try:
        with db.writing(bind_of(request)) as session:
            work(session)
    except audit.AuditError:
        return fragment(request, "partials/alert.html", status_code=503,
                        message="The audit trail could not be written, so nothing "
                                "was decided.")
    except StoreError as failure:
        if type(failure).__name__ == "NotFound":
            raise
        return fragment(request, "partials/alert.html", status_code=409,
                        message=_explain(failure))
    except (DecisionError, BindingError) as failure:
        return fragment(request, "partials/alert.html", status_code=409,
                        message=_explain(failure))
    header = {"HX-Redirect": then} if then else {"HX-Refresh": "true"}
    return Response(status_code=200, headers=header)
