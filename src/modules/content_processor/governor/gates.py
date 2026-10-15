"""DOM-level content gates (FEASIBILITY_AND_GATED_CONTENT.md section 2).

HTTP-level blocks are classified by `block_classifier`. This module looks at
a rendered (or source) document and names the gate that stands between the
visitor and the content: lead-generation form walls, delivery pincode modals,
unavailable products, login walls, age or audience gates. Each finding carries
the matched evidence so a reviewer can see why.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field
from selectolax.parser import HTMLParser, Node

from src.core.schemas import StrictModel

__all__ = ["GateFinding", "GateKind", "detect_gates"]

GateKind = Literal[
    "lead_gen_form_wall",
    "geo_pincode_modal",
    "product_unavailable",
    "login_wall",
    "age_or_audience_gate",
]

_ASSET_WORDS = re.compile(
    r"\b(white ?paper|report|e-?book|guide|case stud(y|ies)|datasheet|data sheet|brochure|"
    r"playbook|research|pdf|infographic|webinar|toolkit)\b",
    re.IGNORECASE,
)
_DOWNLOAD_CTA = re.compile(
    r"\b(download|get (the|your|my|instant) (free )?(copy|report|guide|e-?book|white ?paper|"
    r"access)|access (the|now|report|full)|unlock|read the full|request (the|a) copy|"
    r"send me|get it now)\b",
    re.IGNORECASE,
)
_GATE_PHRASES = re.compile(
    r"(fill (in|out) (the|this) form|complete the form|enter your (business|work) email|"
    r"to download|to access (the|this)|to read (the|this) full|unlock (the|this))",
    re.IGNORECASE,
)
_NEWSLETTER_CTA = re.compile(
    r"\b(subscribe|sign ?up|newsletter|contact (us|sales)|get in touch)\b", re.IGNORECASE
)
_EMAIL_INPUT = re.compile(r"e-?mail", re.IGNORECASE)
_PINCODE = re.compile(
    r"\b(pin ?code|pincode|zip ?code|postal code|delivery location|check (delivery|availability)|"
    r"select your (location|address|city))\b",
    re.IGNORECASE,
)
_PINCODE_INPUT = re.compile(r"pin ?code|zip|postal|pincode", re.IGNORECASE)
_UNAVAILABLE = re.compile(
    r"(currently unavailable|out of stock|no longer available|sold out|this item is not available|"
    r"product is not available|temporarily unavailable|not available (right now|at the moment)|"
    r"we don.t know when or if this item will be back in stock)",
    re.IGNORECASE,
)
_AGE_GATE = re.compile(
    r"(are you (over|at least|older than) (18|19|21)|confirm (that )?you are (18|21)|"
    r"enter your (date of birth|birth ?date) to (enter|continue)|must be (18|21)|"
    r"healthcare professional|medical professional|for professionals only|"
    r"i am a (healthcare|medical))",
    re.IGNORECASE,
)
_SIGNIN_PROMPT = re.compile(
    r"\b((sign|log) ?in (to|before you) (view|access|continue|read|download|see)|"
    r"(sign|log) ?in required|you must (sign|log) ?in)\b",
    re.IGNORECASE,
)
_SIGNIN_MAX_WORDS = 40
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_WS = re.compile(r"\s+")


class GateFinding(StrictModel):
    """One detected gate with its evidence."""

    kind: GateKind
    severity: Literal["blocks_main_content", "partial"]
    evidence: list[str] = Field(default_factory=list)
    selector_hint: str | None = None
    main_word_count: int = Field(ge=0)


def _text(node: Node) -> str:
    return _WS.sub(" ", node.text(deep=True) or "").strip()


def _is_email_input(node: Node) -> bool:
    a = node.attributes
    if (a.get("type") or "").lower() == "email":
        return True
    blob = " ".join(
        str(a.get(k) or "") for k in ("name", "id", "placeholder", "autocomplete", "aria-label")
    )
    return bool(_EMAIL_INPUT.search(blob))


def _is_password_input(node: Node) -> bool:
    return (node.attributes.get("type") or "").lower() == "password"


def _is_pincode_input(node: Node) -> bool:
    a = node.attributes
    blob = " ".join(str(a.get(k) or "") for k in ("name", "id", "placeholder", "aria-label"))
    return bool(_PINCODE_INPUT.search(blob)) or (
        (a.get("maxlength") or "") == "6" and (a.get("inputmode") or "").lower() == "numeric"
    )


def _form_cta(form: Node) -> str:
    parts: list[str] = []
    for node in form.css("button, input[type=submit], a"):
        if node.tag == "input":
            parts.append(str(node.attributes.get("value") or ""))
        else:
            parts.append(_text(node))
    return " ".join(p for p in parts if p)


def _main_text(tree: HTMLParser) -> str:
    main = tree.css_first("main") or tree.css_first("article") or tree.body
    if main is None:
        return ""
    for tag in ("nav", "footer", "header", "script", "style", "noscript", "form"):
        for node in main.css(tag):
            node.decompose()
    return _text(main)


def _teaser_words(tree: HTMLParser) -> int:
    """Words of real content left around a lead-gen form.

    Main text without the form, headings and the sentences that only ask the
    visitor to fill the form or download. `verify.gaps` decides PARTIAL
    (teaser kept) or BLOCKED (nothing else) from this count.
    """
    main = tree.css_first("main") or tree.css_first("article") or tree.body
    if main is None:
        return 0
    for tag in ("nav", "footer", "header", "script", "style", "noscript", "form", *_HEADINGS):
        for node in main.css(tag):
            node.decompose()
    sentences = _SENTENCE.split(_text(main))
    kept = [s for s in sentences if not (_GATE_PHRASES.search(s) or _DOWNLOAD_CTA.search(s))]
    return sum(len(s.split()) for s in kept)


def detect_gates(dom_html: bytes, inner_text: str = "") -> list[GateFinding]:
    """Detect content gates in a document.

    Args:
        dom_html: Rendered or source HTML.
        inner_text: Browser visible text when available (improves modal detection).

    Returns:
        Findings in priority order; empty when no gate is detected.
    """
    tree = HTMLParser(dom_html)
    page_text = _WS.sub(" ", inner_text or (tree.body.text(deep=True) if tree.body else "") or "")
    main_text = _main_text(HTMLParser(dom_html))
    main_words = len(main_text.split())
    findings: list[GateFinding] = []
    severity: Literal["blocks_main_content", "partial"] = (
        "blocks_main_content" if main_words < 120 else "partial"
    )

    # Login wall: a password form and little else.
    for form in tree.css("form"):
        if any(_is_password_input(i) for i in form.css("input")) and main_words < 200:
            findings.append(
                GateFinding(
                    kind="login_wall",
                    severity="blocks_main_content",
                    evidence=["form:password_input", f"main_words={main_words}"],
                    selector_hint=_selector(form),
                    main_word_count=main_words,
                )
            )
            break

    # Sign-in prompt without a form: a tiny page whose content is "sign in to view"
    # (e.g. an HTML login notice served where a document was expected, case 106).
    signin = _SIGNIN_PROMPT.search(main_text)
    if signin and main_words < _SIGNIN_MAX_WORDS and not findings:
        findings.append(
            GateFinding(
                kind="login_wall",
                severity="blocks_main_content",
                evidence=[
                    f"phrase:{signin.group(0)[:40]}",
                    "no_password_input",
                    f"main_words={main_words}",
                ],
                main_word_count=main_words,
            )
        )

    # Lead-generation wall: email input + download/access CTA + asset vocabulary.
    for form in tree.css("form"):
        inputs = form.css("input")
        if not any(_is_email_input(i) for i in inputs):
            continue
        cta = _form_cta(form)
        around = _text(form)
        cta_hit = _DOWNLOAD_CTA.search(cta) or _DOWNLOAD_CTA.search(around)
        gate_hit = _GATE_PHRASES.search(page_text)
        asset_hit = _ASSET_WORDS.search(page_text)
        newsletter_only = _NEWSLETTER_CTA.search(cta) and not cta_hit
        if newsletter_only or not asset_hit or not (cta_hit or gate_hit):
            continue
        evidence = ["form:email_input"]
        if cta_hit:
            evidence.append(f"cta:{cta_hit.group(0)[:40]}")
        if gate_hit:
            evidence.append(f"phrase:{gate_hit.group(0)[:40]}")
        evidence.append(f"asset:{asset_hit.group(0)[:30]}")
        evidence.append(f"main_words={main_words}")
        evidence.append(f"teaser_words={_teaser_words(HTMLParser(dom_html))}")
        findings.append(
            GateFinding(
                kind="lead_gen_form_wall",
                severity=severity,
                evidence=evidence,
                selector_hint=_selector(form),
                main_word_count=main_words,
            )
        )
        break

    # Delivery pincode / location modal.
    pin_inputs = [i for i in tree.css("input") if _is_pincode_input(i)]
    pin_phrase = _PINCODE.search(page_text)
    if pin_inputs and pin_phrase:
        container = _overlay_ancestor(pin_inputs[0])
        findings.append(
            GateFinding(
                kind="geo_pincode_modal",
                severity="partial",
                evidence=[
                    "input:pincode",
                    f"phrase:{pin_phrase.group(0)[:40]}",
                    "overlay" if container else "inline",
                ],
                selector_hint=_selector(container) if container else _selector(pin_inputs[0]),
                main_word_count=main_words,
            )
        )

    # Unavailable product.
    unavailable = _UNAVAILABLE.search(main_text) or _UNAVAILABLE.search(page_text[:4000])
    if unavailable:
        findings.append(
            GateFinding(
                kind="product_unavailable",
                severity="partial",
                evidence=[f"phrase:{unavailable.group(0)[:50]}"],
                main_word_count=main_words,
            )
        )

    # Age / audience gate.
    age = _AGE_GATE.search(page_text[:6000])
    if age:
        findings.append(
            GateFinding(
                kind="age_or_audience_gate",
                severity="blocks_main_content" if main_words < 200 else "partial",
                evidence=[f"phrase:{age.group(0)[:50]}"],
                main_word_count=main_words,
            )
        )
    return findings


def _overlay_ancestor(node: Node) -> Node | None:
    current = node.parent
    depth = 0
    while current is not None and depth < 8:
        a = current.attributes
        parts = (a.get("id"), a.get("class"), a.get("role"), a.get("style"))
        blob = " ".join(str(p or "") for p in parts).lower()
        if (
            "dialog" in blob
            or "modal" in blob
            or "overlay" in blob
            or "position:fixed" in blob.replace(" ", "")
        ):
            return current
        current = current.parent
        depth += 1
    return None


def _selector(node: Node | None) -> str | None:
    if node is None:
        return None
    a = node.attributes
    if a.get("id"):
        return f"#{a.get('id')}"
    cls = (a.get("class") or "").split()
    return f"{node.tag}.{cls[0]}" if cls else node.tag
