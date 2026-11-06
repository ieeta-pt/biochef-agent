"""How the page writes times and digests. All times are UTC, as stored."""

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
          "Nov", "Dec")


def day(moment):
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def minute(moment):
    return f"{day(moment)} {moment:%H:%M}"


def ago(moment, now):
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def short_digest(digest):
    """sha256:4be1…90c3, the first and last four of the hash."""
    kind, colon, value = digest.rpartition(":")
    if len(value) <= 8:
        return digest
    return f"{kind}{colon}{value[:4]}…{value[-4:]}"
