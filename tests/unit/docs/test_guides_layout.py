"""
Every guide prints at its designed size. A command in a `chip wrap` span wraps only when the guide's own CSS
says so; without the rule it stays on one line, runs off the page, and Chromium shrinks the whole PDF page to
fit (the room guide printed at 7 pt, its right column cut off, until the rule was added).
"""

import re
from pathlib import Path

import pytest

GUIDES = Path(__file__).resolve().parents[3] / "docs" / "guides"
INDEXES = sorted(GUIDES.glob("*/index.html"))
WRAP_RULE = re.compile(r"\.chip\.wrap\s*\{[^}]*white-space\s*:\s*normal")


def uses_a_wrapping_chip(html):
    return any({"chip", "wrap"} <= set(classes.split()) for classes in re.findall(r'class="([^"]*)"', html))


def test_the_guides_are_found():
    assert len(INDEXES) >= 5, INDEXES


@pytest.mark.parametrize("index", INDEXES, ids=lambda path: path.parent.name)
def test_every_chip_that_may_wrap_has_the_rule_that_lets_it(index):
    html = index.read_text(encoding="utf-8")
    if uses_a_wrapping_chip(html):
        assert WRAP_RULE.search(html), f"{index.parent.name}: 'chip wrap' is used but .chip.wrap has no white-space: normal rule"


def test_the_guard_sees_a_missing_rule():
    page = '<style>.chip { white-space: nowrap; }</style><span class="chip wrap">a very long command</span>'
    assert uses_a_wrapping_chip(page) and not WRAP_RULE.search(page)
    assert WRAP_RULE.search(page.replace("</style>", ".chip.wrap { white-space: normal; overflow-wrap: anywhere; }</style>"))
    assert not uses_a_wrapping_chip('<span class="chip">sudo systemctl stop croom</span>')
