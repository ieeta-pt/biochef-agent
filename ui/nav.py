"""What the sidebar shows.

The counts stay empty until the screens that own them exist (part 4), and this
does not import the store: a shell with no database behind it still has to open.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    count: str | None = None


ITEMS = (
    Item("Inbox", "/ui/inbox"),
    Item("Studies", "/ui/studies"),
    Item("Requests", "/ui/requests"),
    Item("Datasets", "/ui/datasets"),
    Item("Node", "/ui/node"),
)
