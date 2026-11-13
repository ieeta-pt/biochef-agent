"""Column types of the store, kept apart from the models.

Migrations import from here, so a later change to store/models.py cannot break
a migration that has already run somewhere.
"""

from datetime import timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """A moment in UTC. Refuses a naive datetime rather than guessing its zone.

    SQLite stores no zone at all. Comparing a request's expiry with now is a
    security check, so what goes in has to say what it means.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: the store keeps UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)
