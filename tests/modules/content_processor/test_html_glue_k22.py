"""K22: adjacent text must never be glued together without a separator."""

from __future__ import annotations

import pytest

from src.modules.content_processor.adapters.html_source import extract_html_source


def _texts(html: str) -> list[str]:
    ex = extract_html_source(
        html.encode("utf-8"), base_url="https://x.example/", evidence_id="e", resource_id="r"
    )
    return [b.normalized_text for b in ex.blocks]


@pytest.mark.parametrize(
    ("html", "glued", "expected"),
    [
        # Live John Hancock shapes from the 2026-10-20 run.
        (
            "<p><strong>About John Hancock Investment Management</strong><br>"
            "A company of Manulife Investment Management.</p>",
            "ManagementA",
            [
                "About John Hancock Investment Management",
                "A company of Manulife Investment Management.",
            ],
        ),
        (
            "<td>October 1, 2025<br>Ex Date: September 12, 2025</td>",
            "2025Ex",
            ["October 1, 2025", "Ex Date: September 12, 2025"],
        ),
        # Custom elements wrap whole components.
        (
            "<main><x-card>First card</x-card><x-card>Second card</x-card></main>",
            "cardSecond",
            ["First card", "Second card"],
        ),
        (
            "<dl><dt>Fee</dt><dd>None</dd></dl><figure>Chart</figure><hr>After rule",
            "ChartAfter",
            ["Fee", "None", "Chart", "After rule"],
        ),
    ],
)
def test_no_text_is_glued_across_line_or_component_boundaries(
    html: str, glued: str, expected: list[str]
) -> None:
    texts = _texts(html)
    assert all(glued not in t for t in texts), texts
    assert texts == expected


def test_inline_elements_still_keep_a_sentence_together() -> None:
    """R9 behaviour stays: a link or emphasis inside a sentence does not cut it."""
    texts = _texts('<p>Read the <a href="/terms">full terms</a> before you <b>sign</b>.</p>')
    assert texts == ["Read the full terms before you sign."]


def test_superscript_footnote_is_kept_with_its_number() -> None:
    texts = _texts("<p>The fee is $120<sup>1</sup> a year.</p>")
    assert texts == ["The fee is $120¹ a year."]
