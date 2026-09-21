"""The design system holds — so a restyle is a token change, not a sweep.

The owner's standing rule (2026-09-22): every colour, border and elevation in
the UI comes from a token in `:root`, and repeated pieces are shared
components, so that moving the whole app to another look later means editing
the tokens, not hunting through templates. These tests are what keeps that
true after the person who set it up has stopped paying attention.
"""
import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"
CSS = (WEB / "static" / "app.css").read_text()
TEMPLATES = {p.name: p.read_text() for p in (WEB / "templates").glob("*.html")}

_ROOT_START = CSS.index(":root {")
_ROOT_END = CSS.index("\n}\n", _ROOT_START) + 3
ROOT = CSS[_ROOT_START:_ROOT_END]
OUTSIDE_ROOT = CSS[:_ROOT_START] + CSS[_ROOT_END:]

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")


def test_no_colour_is_written_outside_the_tokens():
    """A literal colour is a place a restyle has to find by hand."""
    found = COLOUR.findall(OUTSIDE_ROOT)
    assert not found, f"colour literals outside :root — make them tokens: {found}"


def test_every_token_used_is_defined():
    """An undefined var() fails silently: --good-bd, --bad-bd and --warn-bd
    were used for months with no definition, and every border that relied on
    them simply did not draw."""
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
    for html in TEMPLATES.values():
        defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", html))
    # var(--x) with no fallback must resolve; var(--x, fallback) is a
    # component parameter a caller MAY set (a bar's height, a lane's colour),
    # and its fallback is what it draws with when nobody does.
    bare = re.compile(r"var\((--[a-z0-9-]+)\)")
    used = set(bare.findall(CSS))
    for html in TEMPLATES.values():
        used |= set(bare.findall(html))
    missing = sorted(used - defined)
    assert not missing, f"tokens used but never defined: {missing}"


# Inline styles are one-off design decisions a restyle cannot reach. There are
# still some from before the rule; the number may only go down. Lower it here
# whenever a page is moved onto components.
INLINE_STYLE_CEILING = 72


def test_inline_styles_only_ever_decrease():
    count = sum(html.count('style="') for html in TEMPLATES.values())
    assert count <= INLINE_STYLE_CEILING, (
        f"{count} inline style attributes, ceiling is {INLINE_STYLE_CEILING} — "
        "use a component class instead of style=\"\"")
