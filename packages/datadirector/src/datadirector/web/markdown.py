"""Markdown that is escaped before it is interpreted.

A statement is a person's own prose, and this interface displays it. Displaying
it as one run of characters is the defect this module exists to fix: a researcher
who wrote three paragraphs, a list of three sites, and a DOI is shown a wall of
text, which is the reading experience of a log file rather than of a document
they wrote.

The obvious dependency was not used. `python-markdown` passes raw HTML through by
default, and raw HTML in this application is not a formatting choice: the same
renderer that displays a researcher's statement displays text that came from a
file, from a model that read a file, and from a person who was told what the file
said. A renderer that admits `<script>` is a renderer that admits whatever the
next deposit contains. So this module escapes on the way in and interprets after,
which makes injection structural rather than filtered: no input reaches the
output as markup, because every `<` is an entity before any pattern runs.

The subset is what prose needs and nothing else: paragraphs, line breaks kept as
the author made them, demoted headings, emphasis, code, lists, blockquotes, and
links. No images: a remote image is fetched by whoever opens the page, which for
an auditor reading a restricted job two years later is a disclosure the
researcher never agreed to. A link whose scheme is not http, https or mailto is
left as the text it is, because a `javascript:` href is not a link that was
mis-typed, it is the thing that gets past a renderer that trusted a scheme list
one entry too long.
"""

from __future__ import annotations

import re
from html import escape, unescape

from markupsafe import Markup

# A statement is not the document's outline. Page headings are h1-h3, so a `#` in
# someone's prose becomes h4 and the outline of the page survives; a screen
# reader navigating by heading should land on the interface's structure, not on
# the middle of a researcher's paragraph.
_HEADING_LEVELS = {1: 4, 2: 4, 3: 5, 4: 5, 5: 6, 6: 6}

# What may be linked. Everything else is left as text, deliberately: an
# unrecognised scheme is not rendered, and not sanitised into something
# renderable.
LINK_SCHEMES = ("http://", "https://", "mailto:")

_CODE_FENCE = re.compile(r"^```[^ ]*\n(.*?)^```[ \t]*$", re.M | re.S)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_BULLET = re.compile(r"^[ \t]*[*+-][ \t]+(.*)$")
_NUMBERED = re.compile(r"^[ \t]*\d+[.)][ \t]+(.*)$")
_QUOTE = re.compile(r"^>[ \t]?(.*)$")
_HR = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$")

_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:[^)]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:[ \t]+[^)]*)?\)")
_CODE = re.compile(r"`([^`]+)`")
_STRONG = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
# `_` inside a word is part of an identifier, not emphasis: a statement that
# lists `site_1` and `site_2` must not come out italicised.
_EMPH = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)"
                   r"|(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])")

# Applied to what a person wrote: the statement, an instruction, a reason given
# for a decision. Not applied to agent prose. A model's text on these pages is
# the thing under review, and letting it format the page that reports on it is
# the wrong direction of trust — the same reason the history page shows a
# model's indicators as sentences rather than as its own markdown.


def render_markdown(text: str | None) -> Markup:
    """Human prose as markup, from a subset that cannot execute anything.

    The return is `Markup` so Jinja does not escape it a second time; nothing
    else in the interface should mark a string safe, and nothing here returns a
    string that was not escaped on its way in.
    """
    if not text:
        return Markup("")
    source = text.replace("\r\n", "\n").replace("\r", "\n")

    parts: list[str] = []
    cursor = 0
    for fence in _CODE_FENCE.finditer(source):
        parts.append(_blocks(source[cursor:fence.start()]))
        parts.append(f"<pre><code>{escape(fence.group(1).rstrip())}\n"
                      "</code></pre>")
        cursor = fence.end()
    parts.append(_blocks(source[cursor:]))
    return Markup("".join(parts))


def _blocks(text: str) -> str:
    """Text split into blocks at blank lines, each block marked up."""
    return "".join(_block(block) for block in text.split("\n\n")
                   if block.strip())


def _block(text: str) -> str:
    """One paragraph-level block, as markup."""
    lines = [line for line in text.split("\n") if line.strip()]
    heading = _HEADING.match(lines[0])
    if heading and len(lines) == 1:
        level = _HEADING_LEVELS[len(heading.group(1))]
        return f"<h{level}>{_inline(heading.group(2))}</h{level}>"
    if len(lines) == 1 and _HR.match(lines[0]):
        return "<hr>"
    quotes = [_QUOTE.match(line) for line in lines]
    if all(quotes):
        inner = [quote.group(1) for quote in quotes]
        return f"<blockquote>{_paragraph(inner)}</blockquote>"
    items = [_BULLET.match(line) or _NUMBERED.match(line) for line in lines]
    if all(items):
        tag = "ol" if any(_NUMBERED.match(line) for line in lines) else "ul"
        return (f"<{tag}>"
                f"{''.join(f'<li>{_inline(m.group(1))}</li>' for m in items)}"
                f"</{tag}>")
    return f"<p>{_paragraph(lines)}</p>"


def _paragraph(lines: list[str]) -> str:
    """Lines as one paragraph, keeping the break the author typed.

    Markdown would join them with a space. A responsibility statement is written
    in a text area by a person who pressed return somewhere, and that return is
    what separated a caveat from the thing it qualifies. Re-flowing it is not a
    typographic improvement, it is an edit to what was said.
    """
    return "<br>".join(_inline(line.rstrip()) for line in lines if line.strip())


def _inline(text: str) -> str:
    """Inline markup, on text escaped on the way in."""
    out = escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _IMAGE.sub(_image, out)
    out = _LINK.sub(_anchor, out)
    out = _STRONG.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>",
                      out)
    return _EMPH.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)


def _anchor(match: re.Match) -> str:
    """A markdown link, if its target is a link at all."""
    label, target = match.group(1), match.group(2)
    if not _linkable(target):
        return match.group(0)
    return (f'<a href="{_href(target)}" rel="noopener noreferrer nofollow">'
            f"{label}</a>")


def _image(match: re.Match) -> str:
    """An image reference, as a link a person chooses to follow.

    An `<img>` is fetched by whoever opens the page. A statement written in 2026
    could then report the presence of the auditor reading it in 2028, to the host
    the researcher pointed at, with the job identifier in the query string if a
    researcher put one there. So nothing is fetched. What the image was labelled,
    and where it pointed, stay visible as a link: the reader is entitled to know
    both, and to decide for themselves.
    """
    label, target = match.group(1), match.group(2)
    if not _linkable(target):
        return match.group(0)
    said = label or "image"
    return (f'<a href="{_href(target)}" rel="noopener noreferrer nofollow">'
            f"[image not loaded: {said}]</a>")


def _href(target: str) -> str:
    """A link target as it may sit inside an attribute.

    Round-tripped through unescape because it arrived from `_inline`, which had
    already escaped it: escaping a second time is what turned a DOI link's
    `?p=1&q=2` into `?p=1&amp;amp;q=2` on the page.
    """
    return escape(unescape(target), quote=True)


def _linkable(target: str) -> bool:
    """Whether a link target is a link at all.

    Unescaped first, because a browser decodes entities in an attribute before it
    acts on the value: `&#106;avascript:alert(1)` is a `javascript:` href wearing
    the one encoding that a check against the literal spelling misses. Then
    lower-cased and stripped of the whitespace a browser would ignore, for the
    same reason — `JaVaScRiPt:` and ` javascript:` are the forms that get past a
    check written against one spelling.
    """
    probe = unescape(target).strip().lower().replace("\x00", "")
    return probe.startswith(LINK_SCHEMES)
