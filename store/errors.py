"""What the store refuses, as exceptions a caller can tell apart."""


class StoreError(Exception):
    """Base class, so a caller can catch everything the store refuses."""


class NotFound(StoreError):
    """No such row."""


class Conflict(StoreError):
    """The write disagrees with what is already recorded."""


class IllegalTransition(StoreError):
    """A state change the machine does not permit."""


class IdentityNotVerified(StoreError):
    """A study version with a member whose identity did not verify."""


class AlreadyPosted(StoreError):
    """The decision has left this node, so it can no longer be undone."""


class StaleList(StoreError):
    """A federation node list older than the newest one stored."""


class ImmutableField(StoreError):
    """A write to a field that signatures or history depend on."""
