"""Tiny whitelist Markdown renderer for LLM-authored thread messages.

Escape-first, then decorate: the whole text goes through markupsafe.escape()
BEFORE any regex runs, so every transform below operates on already-safe text
and can only ever add tags we chose. Deliberately supports NO link syntax —
bare URLs and [text](url) stay plain escaped text, which keeps the whole
[x](javascript:...) class of holes closed. Deterministic, no state, and
markupsafe is already a Jinja2 dependency — no new packages.
"""
import re
from markupsafe import Markup, escape

# \x00 / \x01 sentinels cannot appear in escaped text, so stashed blocks can
# never collide with real content or with each other.
_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.S)          # ```lang\n...```
_CODE_RE = re.compile(r"`([^`\n]+)`")                          # `inline`
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")           # **bold**
# *italic* — word-boundary safe: needs a real closer on the same line, so a
# lone * (e.g. "3 * 4") never eats the rest of the line.
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")                   # any # depth
_UL_RE = re.compile(r"^-\s+(.+)$")                             # "- item"
_OL_RE = re.compile(r"^\d+\.\s+(.+)$")                         # "1. item"
_FENCE_SLOT_RE = re.compile(r"^\x00(\d+)\x00$")


def render_md(text) -> Markup:
    """Render a trusted-shape, untrusted-content string to safe HTML."""
    src = str(escape(text or ""))

    # 1) Pull fenced code blocks out first so nothing inside them is styled;
    #    they come back last as <pre><code> blocks.
    fences: list = []

    def _stash_fence(m):
        fences.append(m.group(1))
        return f"\n\x00{len(fences) - 1}\x00\n"          # own line → block-level

    src = _FENCE_RE.sub(_stash_fence, src)

    # 2) Same trick for inline code, so bold/italic can't mangle `a * b`.
    codes: list = []

    def _stash_code(m):
        codes.append(m.group(1))
        return f"\x01{len(codes) - 1}\x01"

    src = _CODE_RE.sub(_stash_code, src)

    # 3) Inline emphasis (order matters: ** before *).
    src = _BOLD_RE.sub(r"<strong>\1</strong>", src)
    src = _ITALIC_RE.sub(r"<em>\1</em>", src)

    # 4) Line walk for block structure: headings, lists, paragraphs.
    out: list = []
    para: list = []          # accumulated plain lines → one <p>, <br>-joined
    list_tag = None          # currently open "ul" / "ol", if any

    def _close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def _flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def _open_list(tag, item):
        nonlocal list_tag
        _flush_para()
        if list_tag != tag:
            _close_list()
            out.append(f"<{tag}>")
            list_tag = tag
        out.append(f"<li>{item}</li>")

    for line in src.split("\n"):
        slot = _FENCE_SLOT_RE.match(line.strip())
        heading = _HEADING_RE.match(line)
        ul, ol = _UL_RE.match(line), _OL_RE.match(line)
        if slot:                                   # restore a fenced block
            _flush_para(); _close_list()
            out.append(f"<pre><code>{fences[int(slot.group(1))]}</code></pre>")
        elif heading:                              # all levels cap at small h4
            _flush_para(); _close_list()
            out.append(f'<h4 class="md-h">{heading.group(1)}</h4>')
        elif ul:
            _open_list("ul", ul.group(1))
        elif ol:
            _open_list("ol", ol.group(1))
        elif not line.strip():                     # blank → paragraph break
            _flush_para(); _close_list()
        else:                                      # plain text line
            _close_list()
            para.append(line)
    _flush_para(); _close_list()

    # 5) Re-insert inline code (contents were escaped in step 0, kept verbatim).
    html = re.sub(r"\x01(\d+)\x01", lambda m: f"<code>{codes[int(m.group(1))]}</code>",
                  "\n".join(out))
    return Markup(html)
