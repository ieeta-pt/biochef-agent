"""The sidebar's items. Their counts come from ui/views.py."""

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


def items(counts):
    return [Item(item.label, item.href, counts.get(item.label)) for item in ITEMS]
