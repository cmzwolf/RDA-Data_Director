"""The markdown subset used for a person's own prose.

The renderer's reason for existing is that the statements it displays are not
trusted text: they arrive from a textarea, and the same screen later shows what a
model made of them. So these tests are less about whether a heading is produced
than about what cannot be.
"""

from __future__ import annotations

from markupsafe import Markup

from datadirector.web.markdown import render_markdown


def test_a_statement_that_was_never_written_renders_nothing():
    assert render_markdown("") == Markup("")
    assert render_markdown(None) == Markup("")
    assert render_markdown("\n\n    \n") == Markup("")


def test_the_break_the_author_typed_survives():
    """A caveat separated by a return stays separated.

    Markdown proper joins the lines with a space. The person writing in a text
    area pressed return for a reason, and re-flowing the text moves the caveat
    away from the thing it qualifies.
    """
    out = render_markdown("Sites: Riverbank and Meadow.\nThe first is higher.")
    assert out == Markup("<p>Sites: Riverbank and Meadow.<br>"
                            "The first is higher.</p>")


def test_separated_paragraphs_stay_separate():
    assert render_markdown("Said one thing.\n\nSaid another.") == Markup(
        "<p>Said one thing.</p><p>Said another.</p>")


def test_emphasis_and_code_as_a_person_writes_them():
    out = render_markdown("Consent **excludes** quotation; use `director`.")
    assert out == Markup("<p>Consent <strong>excludes</strong> quotation; "
                            "use <code>director</code>.</p>")


def test_a_field_name_is_not_emphasis():
    """`site_1` and `site_2` are two sites, not an italic run.

    An emphasis rule without lookarounds italicises the underscores in a field or
    site list, which changes what a reader sees for a reason that has nothing to
    do with what was said.
    """
    out = render_markdown("plot site_1 against site_2 for the core counts")
    assert "<em>" not in out


def test_a_list_the_researcher_made_is_a_list():
    assert render_markdown("- Riverbank\n- Meadow") == Markup(
        "<ul><li>Riverbank</li><li>Meadow</li></ul>")


def test_a_numbered_list_is_not_a_bulleted_one():
    out = render_markdown("1. collect\n2. review\n3. deposit")
    assert out.startswith("<ol>") and "<li>review</li>" in out


def test_a_blockquote_stays_a_blockquote():
    assert render_markdown("> do not quote participants") == Markup(
        "<blockquote>do not quote participants</blockquote>")


def test_a_heading_is_demoted_below_the_pages_own():
    """A statement does not take over the document outline.

    Someone navigating a page by heading should hear the interface's structure,
    not a heading from the middle of a researcher's paragraph.
    """
    assert render_markdown("# Methods") == Markup("<h4>Methods</h4>")
    assert render_markdown("### Sites") == Markup("<h5>Sites</h5>")


def test_a_fenced_block_keeps_its_content_as_text():
    out = render_markdown("```\n<a href='x'>\n```")
    assert out.startswith("<pre><code>")
    assert "<a href" not in out
    assert "&lt;a href" in out



# What follows is about what the renderer refuses rather than produces. The text
# it renders arrives from a textarea, and the screen that shows it is reachable
# by an auditor two years later.

def test_raw_html_in_a_statement_cannot_become_markup():
    """The renderer escapes on the way in, so there is no path through.

    The alternative — render first, sanitise afterwards — depends on the
    sanitiser having anticipated every construction that reaches it.
    """
    out = render_markdown("Before <script>alert(1)</script> after")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_a_javascript_link_is_left_as_the_text_it_is():
    out = render_markdown("[open this](javascript:alert(1))")
    assert "<a " not in out
    assert "javascript:alert(1)" in out        # visible, not quietly removed


def test_an_entity_encoded_javascript_link_is_still_refused():
    """`&#106;avascript:` is the form that gets past a literal check.

    A browser decodes entities in an attribute before it acts on the value, so a
    check against the spelling in the source has checked the wrong string.
    """
    assert "<a " not in render_markdown("[x](&#106;avascript:alert(1))")


def test_a_mixed_case_javascript_link_is_still_refused():
    assert "<a " not in render_markdown("[x](JaVaScRiPt:alert(1))")


def test_a_data_link_is_refused():
    assert "<a " not in render_markdown("[x](data:text/html;base64,PHNjcmlwdD4=)")


def test_a_mailto_is_a_link():
    """A person leaving a contact is why the scheme list exists."""
    out = render_markdown("[the depositor](mailto:someone@example.test)")
    assert '<a href="mailto:someone@example.test"' in out


def test_an_accepted_link_carries_its_target_once():
    """An ampersand in a query string is not escaped twice into `&amp;amp;`.

    A DOI pasted into a statement is the ordinary case, and a link that does not
    resolve because of how it was written out is a failure the researcher sees
    and cannot explain.
    """
    out = render_markdown("[the record](https://doi.org/10.5281/zenodo.1234567)"
                          " [params](https://x.test/?p=1&q=2)")
    assert '<a href="https://doi.org/10.5281/zenodo.1234567"' in out
    assert 'href="https://x.test/?p=1&amp;q=2"' in out
    assert "&amp;amp;" not in out


def test_an_image_is_not_loaded_and_is_not_lost():
    """Nothing is fetched, and the reader is told what was withheld.

    An `<img>` is requested by whoever opens the page, so a statement written in
    2026 could report the presence of the auditor reading it in 2028 — to the
    host the researcher pointed at, with whatever was put in the query string.
    The label and the destination stay visible, because silently deleting the
    reference would leave the reader with a text that is not the one written.
    """
    out = render_markdown("![the site map](https://evil.example/t.png?j=job-01)")
    assert "<img" not in out
    assert "image not loaded: the site map" in out
    assert 'href="https://evil.example/t.png?j=job-01"' in out


def test_the_filter_is_registered_on_the_template_environment():
    """`| md` in a template is the only way text is marked safe.

    Registered once, in `templates_for()`, which the interface and the sign-in
    pages share: an environment built elsewhere would render the same statement
    as plain text, and nothing here would notice.
    """
    from datadirector.web.views import templates_for

    templates = templates_for()
    assert templates.env.filters["md"] is render_markdown
    assert templates.env.filters["md"]("**said**") == Markup(
         "<p><strong>said</strong></p>")
