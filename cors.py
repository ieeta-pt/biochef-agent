"""Which browser origins may call this service (#74).

The editor runs on one origin and the agent on another, so a call from the page
is cross-origin, and without CORS headers the browser withholds the response.
For `POST /convert` that is worse than a refusal: multipart is a safelisted
content type, so there is no preflight, the agent runs the whole workflow, and
only the answer is lost.

Closed by default. An empty setting adds no CORS headers at all, which is what
this service did before. Origins are exact, and `*` is refused: this service
executes tool binaries, and which pages may drive it is a decision a deployment
makes by name, not one it inherits from a wildcard.
"""

import os
import re

ORIGIN = re.compile(r"\Ahttps?://[A-Za-z0-9.-]+(:[0-9]{1,5})?\Z")
"""scheme://host[:port] and nothing else.

A browser sends `Origin` with no path and no trailing slash, so a configured
`http://localhost:3000/` would never match and the deployment would look
configured while refusing every call. Refused at startup instead.
"""


def get_cors_origins(value=None):
    """The configured origins, or [] when none are -- refusing to start on a bad one."""
    raw = os.getenv("BIOCHEF_CORS_ORIGINS", "") if value is None else value
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    for origin in origins:
        if origin == "*":
            raise ValueError(
                "BIOCHEF_CORS_ORIGINS does not accept '*'. Name the origins "
                "that may call this service, e.g. http://localhost:3000."
            )
        if not ORIGIN.match(origin):
            raise ValueError(
                f"BIOCHEF_CORS_ORIGINS entry {origin!r} is not an origin. "
                f"Use scheme://host[:port] with no path or trailing slash, "
                f"exactly as a browser sends it."
            )
    return origins


def middleware_options(origins):
    """What CORSMiddleware is given. One place, so main and its tests agree."""
    return dict(
        allow_origins=origins,
        allow_methods=["GET", "HEAD", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        # Retry-After rides on the 503 at capacity; a page needs it to back off.
        expose_headers=["Retry-After"],
        # Bearer travels in a header, not a cookie, so credentials mode is not
        # needed -- and leaving it off keeps cookies out of it entirely.
        allow_credentials=False,
    )
