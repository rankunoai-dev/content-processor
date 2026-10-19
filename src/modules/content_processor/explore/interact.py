"""Page-side scripts for the interactive controls (B-4 to B-7).

- B-4 carousels: the "next" control of each carousel is clicked until the
  carousel's text repeats (it cycled) or the declared slide count is reached.
- B-5 hover/focus: tooltip, popover and hover-menu triggers are hovered; Tab
  traversal walks the focus order once and keeps any text focus reveals.
- B-6 dialogs and consent: informational dialogs are opened, captured and
  closed again (Escape, then a close button). A consent overlay is answered
  with the declared choice, reject non-essential; when the banner offers only
  "accept" it is left in place, because accepting is not ours to decide.
- B-7 pagination and load-more: in-page controls are clicked until they
  disappear or are disabled; "next" links that are real URLs are left to the
  frontier as follow-ups. The explorer's GET-only guard applies to every click.
"""

from __future__ import annotations

__all__ = [
    "CAROUSEL_TEXT_JS",
    "CLOSE_DIALOGS_JS",
    "CONSENT_JS",
    "CONSENT_VISIBLE_JS",
    "CONTROL_STATE_JS",
    "FOCUS_JS",
    "MAX_ITERATIONS",
]

MAX_ITERATIONS = 25

_CAROUSEL = (
    '[aria-roledescription="carousel"],.carousel,.swiper,.swiper-container,.slick-slider,'
    ".glide,.splide,[data-carousel]"
)

CONSENT_JS = """
() => {
  const banners = ['#onetrust-banner-sdk', '#CybotCookiebotDialog', '#truste-consent-track',
    '#didomi-notice', '.qc-cmp2-summary-section', '.cc-window', '#cookie-banner', '#cookieBanner',
    '.cookie-banner', '#cookie-consent', '.cookie-consent', '[data-cp-consent-banner]',
    '[role="dialog"][aria-label*="cookie" i]', '[role="dialog"][aria-label*="consent" i]',
    '[role="region"][aria-label*="cookie" i]', '[aria-label*="cookie consent" i]'];
  const reject = new RegExp('^\\\\s*(reject|decline|deny|refuse)'
    + '( all| cookies| non-essential)?\\\\s*$'
    + '|only (necessary|essential)|(necessary|essential) (cookies )?only|use necessary', 'i');
  const accept = /accept|agree|allow|got it|^\\s*ok\\s*$/i;
  const shown = (el) => (el.checkVisibility ? el.checkVisibility() : !!el.getClientRects().length);
  for (const sel of banners) {
    const banner = document.querySelector(sel);
    if (!banner || !shown(banner)) { continue; }
    banner.setAttribute('data-cp-consent-banner', '1');
    const buttons = Array.from(banner.querySelectorAll('button,[role=button],a,input[type=button]'))
      .filter(shown);
    const label = (b) => (b.getAttribute('aria-label') || b.innerText || b.value || '').trim();
    const choice = buttons.find((b) => reject.test(label(b)) || /reject|decline/i.test(b.id));
    if (choice) { choice.setAttribute('data-cp-consent', '1'); }
    return {
      found: true,
      label: (banner.getAttribute('aria-label') || banner.id || 'consent banner').slice(0, 60),
      choice: choice ? label(choice).slice(0, 60) : null,
      acceptOnly: !choice && buttons.some((b) => accept.test(label(b))),
    };
  }
  return { found: false };
}
"""

CONSENT_VISIBLE_JS = """
() => {
  const b = document.querySelector('[data-cp-consent-banner]');
  return !!b && (b.checkVisibility ? b.checkVisibility() : !!b.getClientRects().length);
}
"""

CAROUSEL_TEXT_JS = (
    """
(id) => {
  const el = document.querySelector(`[data-cp-ctl="${id}"]`);
  const box = el ? el.closest('"""
    + _CAROUSEL.replace("'", "\\'")
    + """') : null;
  return box ? box.innerText : null;
}
"""
)

CONTROL_STATE_JS = """
(id) => {
  const el = document.querySelector(`[data-cp-ctl="${id}"]`);
  if (!el) { return { present: false }; }
  const shown = el.checkVisibility ? el.checkVisibility() : !!el.getClientRects().length;
  const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true'
    || el.classList.contains('disabled');
  return { present: true, visible: shown, disabled };
}
"""

CLOSE_DIALOGS_JS = """
() => {
  const shown = (el) => (el.checkVisibility ? el.checkVisibility() : !!el.getClientRects().length);
  const open = Array.from(document.querySelectorAll(
    'dialog[open],[role="dialog"],[role="alertdialog"],[aria-modal="true"],.modal.show'))
    .filter((d) => shown(d) && !d.hasAttribute('data-cp-consent-banner'));
  for (const d of open) {
    if (d.tagName === 'DIALOG' && typeof d.close === 'function') { d.close(); continue; }
    const btn = d.querySelector('[aria-label*="close" i],[data-dismiss="modal"],'
      + '[data-bs-dismiss="modal"],.close,.btn-close,[data-a11y-dialog-hide]');
    if (btn) { btn.click(); }
  }
  return open.filter(shown).length;
}
"""

# Marks the focused element so a full focus cycle can be recognised.
FOCUS_JS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) { return null; }
  if (!el.hasAttribute('data-cp-focus')) {
    const body = document.body;
    const next = Number(body.getAttribute('data-cp-focus-next') || '0');
    el.setAttribute('data-cp-focus', String(next));
    body.setAttribute('data-cp-focus-next', String(next + 1));
  }
  return { id: el.getAttribute('data-cp-focus'),
           label: (el.getAttribute('aria-label') || el.innerText || el.tagName)
             .trim().slice(0, 60) };
}
"""

DISCOVER_EXTRA_JS = (
    """
  const carouselSel = '"""
    + _CAROUSEL
    + """';
  const nextSel = '[aria-label*="next" i],.next,.carousel-control-next,.swiper-button-next,'
    + '.slick-next,.glide__arrow--right,.splide__arrow--next,[data-slide="next"],'
    + '[data-bs-slide="next"]';
  const slideSel = '[aria-roledescription="slide"],.carousel-item,'
    + '.swiper-slide:not(.swiper-slide-duplicate),.slick-slide:not(.slick-cloned),'
    + '.glide__slide:not(.glide__slide--clone),.splide__slide:not(.is-clone)';
  document.querySelectorAll(carouselSel).forEach((c) => {
    if (c.parentElement && c.parentElement.closest(carouselSel)) { return; }
    const n = c.querySelector(nextSel);
    if (n) { tag(n, 'carousel', { total: c.querySelectorAll(slideSel).length }); }
  });
  const moreRe = new RegExp('^\\\\s*(load|show|view|see)\\\\s+(more|all)\\\\b'
    + '|^\\\\s*more\\\\s+(results|items|products|reviews|stories|posts)\\\\b', 'i');
  document.querySelectorAll('button,[role=button],a').forEach((el) => {
    const l = labelOf(el);
    if (l.length <= 40 && moreRe.test(l)) { tag(el, 'load_more'); }
  });
  document.querySelectorAll('a[rel~="next"],[aria-label*="next page" i]')
    .forEach((el) => tag(el, 'pagination'));
  document.querySelectorAll('.pagination,nav[aria-label*="pagination" i],'
    + '[role=navigation][aria-label*="pagination" i]').forEach((nav) => {
    nav.querySelectorAll('a,button').forEach((el) => {
      if (/^(next|next page|\\u203a|\\u00bb|>)$/i.test(labelOf(el))) { tag(el, 'pagination'); }
    });
  });
  document.querySelectorAll('[data-tooltip],[data-tip],[data-bs-toggle="tooltip"],'
    + '[data-toggle="tooltip"],[data-bs-toggle="popover"],[data-toggle="popover"]')
    .forEach((el) => tag(el, 'hover'));
  document.querySelectorAll('[aria-describedby]').forEach((el) => {
    const ids = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
    const targets = ids.map((i) => document.getElementById(i)).filter(Boolean);
    if (targets.length && targets.some((t) => !isVisible(t))) { tag(el, 'hover'); }
  });
  document.querySelectorAll('nav li,[role=menubar] > li,[role=menubar] > [role=none]')
    .forEach((li) => {
      const sub = li.querySelector(':scope > ul, :scope > div, :scope > [role=menu]');
      if (sub && !isVisible(sub) && !li.querySelector('[aria-expanded]')) { tag(li, 'hover'); }
    });
"""
)

DISCOVER_DIALOG_JS = """
  document.querySelectorAll('[aria-haspopup="dialog"],[data-toggle="modal"],[data-bs-toggle="modal"],'
    + '[data-modal-target],[data-a11y-dialog-show],button[commandfor]').forEach((el) => {
    tag(el, 'dialog');
  });
  document.querySelectorAll('[aria-controls]').forEach((el) => {
    const t = document.getElementById(el.getAttribute('aria-controls'));
    if (t && (t.tagName === 'DIALOG' || t.getAttribute('role') === 'dialog')) { tag(el, 'dialog'); }
  });
"""
