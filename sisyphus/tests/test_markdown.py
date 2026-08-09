"""Tests for web/markdown.py — the whitelist renderer for LLM-authored text."""
from markupsafe import Markup

from web.markdown import render_md


class TestEscaping:
    def test_script_tag_becomes_text(self):
        html = str(render_md("<script>alert(1)</script>"))
        assert "<script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_html_inside_code_is_escaped(self):
        html = str(render_md("`<img src=x onerror=alert(1)>`"))
        assert "<img" not in html
        assert "<code>&lt;img" in html

    def test_returns_markup(self):
        assert isinstance(render_md("hi"), Markup)

    def test_empty_and_none_safe(self):
        assert str(render_md("")) == ""
        assert str(render_md(None)) == ""


class TestInline:
    def test_bold(self):
        assert "<strong>big</strong>" in str(render_md("a **big** deal"))

    def test_italic(self):
        assert "<em>slanted</em>" in str(render_md("a *slanted* word"))

    def test_lone_star_does_not_eat_line(self):
        html = str(render_md("3 * 4 is twelve"))
        assert "<em>" not in html
        assert "3 * 4 is twelve" in html

    def test_inline_code(self):
        assert "<code>x = 1</code>" in str(render_md("set `x = 1` first"))

    def test_stars_inside_inline_code_untouched(self):
        html = str(render_md("`a * b * c`"))
        assert "<code>a * b * c</code>" in html
        assert "<em>" not in html


class TestBlocks:
    def test_fenced_code_block(self):
        html = str(render_md("before\n```python\nprint('hi')\n```\nafter"))
        assert "<pre><code>print(&#39;hi&#39;)" in html
        assert "before" in html and "after" in html

    def test_markdown_inside_fence_not_rendered(self):
        html = str(render_md("```\n**not bold**\n# not a heading\n```"))
        assert "<strong>" not in html
        assert "<h4" not in html

    def test_headings_capped_to_h4(self):
        for prefix in ("#", "##", "###", "######"):
            html = str(render_md(f"{prefix} Title"))
            assert '<h4 class="md-h">Title</h4>' in html
            for tag in ("<h1", "<h2", "<h3"):
                assert tag not in html

    def test_bullet_list(self):
        html = str(render_md("- one\n- two"))
        assert "<ul>" in html and html.count("<li>") == 2
        assert "<li>one</li>" in html and "<li>two</li>" in html

    def test_ordered_list(self):
        html = str(render_md("1. first\n2. second"))
        assert "<ol>" in html and "<li>first</li>" in html and "<li>second</li>" in html

    def test_paragraphs_and_breaks(self):
        html = str(render_md("line one\nline two\n\nnew para"))
        assert "<p>line one<br>line two</p>" in html
        assert "<p>new para</p>" in html


class TestNoLinks:
    def test_link_syntax_stays_literal(self):
        html = str(render_md("[x](javascript:alert(1))"))
        assert "<a" not in html
        assert "href" not in html
        assert "[x](javascript:alert(1))" in html

    def test_bare_url_stays_plain(self):
        html = str(render_md("see https://example.com/page"))
        assert "<a" not in html
        assert "https://example.com/page" in html
