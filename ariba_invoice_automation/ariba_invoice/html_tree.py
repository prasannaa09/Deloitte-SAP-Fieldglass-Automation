"""Minimal, dependency-free HTML tree for AribaWeb pages.

AribaWeb pages are server-rendered and fairly well formed, but use unquoted attributes and
some unclosed tags. This builds a lightweight element tree with the standard-library parser
and offers the few queries the invoice flow needs (by id, by tag/attr, closest ancestor, text).
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Iterator

FORM_CONTROLS = {"input", "textarea", "select", "button"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# tag -> (open tags it implicitly closes, tags that bound the search). E.g. a new <td> closes an
# unclosed sibling <td> but never its own <tr>; a new <tr> closes an unclosed <tr> within the same table.
AUTO_CLOSE = {
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr"}, {"table", "tbody", "thead", "tfoot"}),
    "li": ({"li"}, {"ul", "ol"}),
    "option": ({"option"}, {"select", "optgroup"}),
    "p": ({"p"}, {"div", "td", "th", "li", "form", "body"}),
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "form")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list["Node | str"] = []
        self.parent = parent
        self.form: "Node | None" = None  # owning <form> (HTML "form element pointer"), for form controls

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def id(self) -> str | None:
        return self.attrs.get("id")

    def iter(self) -> Iterator["Node"]:
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def find_all(self, pred: Callable[["Node"], bool]) -> list["Node"]:
        return [n for n in self.iter() if pred(n)]

    def find(self, pred: Callable[["Node"], bool]) -> "Node | None":
        return next((n for n in self.iter() if pred(n)), None)

    def closest(self, tag: str) -> "Node | None":
        p = self.parent
        while p is not None and p.tag != tag:
            p = p.parent
        return p

    def text(self) -> str:
        parts: list[str] = []

        def walk(n: Node) -> None:
            if n.tag in ("script", "style"):
                return
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                else:
                    walk(c)

        walk(self)
        return re.sub(r"\s+", " ", unescape("".join(parts))).strip()

    def __repr__(self) -> str:
        return f"<{self.tag} id={self.id!r}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.form: Node | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rule = AUTO_CLOSE.get(tag)
        if rule:
            closes, bounds = rule
            n = self.cur
            while n is not self.root and n.tag not in bounds:
                if n.tag in closes:
                    self.cur = n.parent or self.root
                    break
                n = n.parent or self.root
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        self._own(node)
        if tag not in VOID:
            self.cur = node

    def _own(self, node: Node) -> None:
        # Browsers keep a form pointer: controls belong to the last opened <form> until </form>,
        # even when sloppy markup closes the <form> element early in the tree.
        if node.tag == "form":
            self.form = node
        elif node.tag in FORM_CONTROLS:
            node.form = self.form

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        self._own(node)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form = None
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent or self.root
        if n is not self.root:
            self.cur = n.parent or self.root

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root
