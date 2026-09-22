"""The design system holds — so a restyle is a token change, not a sweep.

The owner's standing rule (2026-09-22): every colour, size, shape and motion in
the UI comes from a token, and repeated pieces are shared components, so that
moving the whole app to another look means editing token values — the light
theme in app.css is that, done once. These tests are what keeps it true after
the person who set it up has stopped paying attention.
"""
import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"
CSS = (WEB / "static" / "app.css").read_text()
TEMPLATES = {p.name: p.read_text() for p in (WEB / "templates").glob("*.html")}

# Token blocks: the base `:root { … }` and every `:root[data-theme="…"] { … }`.
_BLOCK = re.compile(r"^:root(\[data-theme=\"[a-z-]+\"\])? \{\n(.*?)\n\}\n", re.S | re.M)
TOKEN_BLOCKS = [(m.group(1) or "", m.group(2)) for m in _BLOCK.finditer(CSS)]
# @font-face declares the typefaces the tokens then point at, so it is the one
# place that names a family and a weight outright.
_FACE = re.compile(r"@font-face\s*\{[^}]*\}", re.S)
RULES = _FACE.sub("", _BLOCK.sub("", CSS))
DEFINED = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")


def _decls(prop_re: str):
    """(property, value) for every declaration in the rules outside the tokens."""
    return [(p, v.strip()) for p, v in
            re.findall(r"(?<![\w-])(" + prop_re + r")\s*:([^;{}]+)(?=[;}])", RULES)]


def test_there_is_a_base_token_set_and_at_least_one_theme():
    themes = [t for t, _ in TOKEN_BLOCKS]
    assert "" in themes and any(themes), themes


def test_a_theme_is_nothing_but_token_values():
    """A theme that styles a selector is a second stylesheet in disguise."""
    base = {n for t, body in TOKEN_BLOCKS if not t for n in re.findall(r"(--[a-z0-9-]+)\s*:", body)}
    for theme, body in TOKEN_BLOCKS:
        if not theme:
            continue
        for name, _ in re.findall(r"([a-z-][a-z0-9-]*)\s*:([^;]+);", body):
            assert name.startswith("--") or name == "color-scheme", f"{theme} sets {name}"
            if name.startswith("--"):
                assert name in base, f"{theme} invents {name}; define it in the base :root first"


def test_every_colour_token_is_retuned_by_every_theme():
    """A colour a theme forgets keeps the dark value — dark text on a light page."""
    base = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", TOKEN_BLOCKS[0][1]))
    colour_tokens = {n for n, v in base.items() if COLOUR.search(v)}
    for theme, body in TOKEN_BLOCKS[1:]:
        set_here = set(re.findall(r"(--[a-z0-9-]+)\s*:", body))
        assert not colour_tokens - set_here, f"{theme} leaves {sorted(colour_tokens - set_here)} dark"


def test_no_colour_is_written_outside_the_tokens():
    found = COLOUR.findall(RULES)
    assert not found, f"colour literals outside the token blocks — make them tokens: {found}"


def test_every_token_used_is_defined():
    """An undefined var() fails silently: --good-bd, --bad-bd and --warn-bd
    were used for months with no definition, and every border that relied on
    them simply did not draw. var(--x, fallback) is a component parameter a
    caller MAY set, and is exempt."""
    defined = set(DEFINED)
    for html in TEMPLATES.values():
        defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", html))
    bare = re.compile(r"var\((--[a-z0-9-]+)\)")
    used = set(bare.findall(CSS))
    for html in TEMPLATES.values():
        used |= set(bare.findall(html))
    missing = sorted(used - defined)
    assert not missing, f"tokens used but never defined: {missing}"


# ── sizes, shapes and motion come from the scales ─────────────────────────

def _raw(decls, allowed):
    return [f"{p}: {v}" for p, v in decls if not v.startswith("var(") and v not in allowed]


def test_type_is_on_the_scale():
    assert not _raw(_decls("font-size"), {"inherit", "1em", ".92em", ".9em", ".85em", "100%"})
    assert not _raw(_decls("font-weight"), {"inherit", "normal"})
    assert not _raw(_decls("line-height"), {"1", "0", "inherit", "normal"})
    assert not _raw(_decls("letter-spacing"), {"0", "normal"})


def test_font_families_are_tokens():
    assert not _raw(_decls("font-family"), {"inherit"})


def test_spacing_is_on_the_scale():
    """0 and a 1px hairline are structure, not spacing; everything else is a step."""
    bad = []
    for p, v in _decls(r"(?:padding|margin)(?:-[a-z]+)*|gap|row-gap|column-gap"):
        for n in re.findall(r"(?<![\w.(-])-?(\d+(?:\.\d+)?)px", v):
            if n not in ("0", "1"):
                bad.append(f"{p}: {v}")
    assert not bad, bad


def test_corners_are_on_the_scale():
    bad = [f"{p}: {v}" for p, v in _decls(r"border(?:-[a-z]+)*-radius")
           if re.search(r"(?<![\w.-])(?!0px)\d+(?:\.\d+)?px", v)]
    assert not bad, bad


def test_motion_is_on_the_scale():
    bad = [f"{p}: {v}" for p, v in _decls("transition|animation|transition-duration|animation-duration")
           if re.search(r"(?<![\w.-])\d*\.?\d+m?s\b", v)]
    assert not bad, bad


def test_stacking_uses_named_layers():
    bad = [f"{p}: {v}" for p, v in _decls("z-index") if not v.startswith("var(") and int(v) > 2]
    assert not bad, bad


# ── templates carry no design decisions of their own ──────────────────────

def test_templates_have_no_style_blocks():
    assert not [n for n, h in TEMPLATES.items() if "<style" in h]


def test_style_attributes_only_pass_parameters():
    """A style attribute may hand a component a value only the server knows
    (a bar's width) as a custom property; it may not style anything itself."""
    bad = []
    for name, html in TEMPLATES.items():
        for body in re.findall(r'style="([^"]*)"', html):
            for decl in filter(None, (d.strip() for d in body.split(";"))):
                if not decl.startswith("--"):
                    bad.append(f"{name}: {decl}")
    assert not bad, bad


def test_scripts_do_not_style_elements():
    """Show/hide goes through `hidden` or a class; only custom properties may be set."""
    bad = []
    for name, html in TEMPLATES.items():
        for line in html.splitlines():
            if re.search(r"\.style\.(?!setProperty\(\s*['\"]--)", line) or re.search(r"\.style\s*=", line):
                bad.append(f"{name}: {line.strip()}")
    assert not bad, bad


def test_templates_write_no_colours():
    bad = []
    for name, html in TEMPLATES.items():
        for body in re.findall(r'style="([^"]*)"', html):
            bad += [f"{name}: {c}" for c in COLOUR.findall(body)]
    assert not bad, bad
