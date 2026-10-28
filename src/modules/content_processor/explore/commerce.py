"""Page-side scripts for product variants and listing filters (B-14).

- `select_variant`: a product page's option pickers (a `<select>` of colours,
  a group of size buttons, swatches, radio inputs) are discovered as
  dimensions. Each option is chosen on its own, one dimension at a time, and
  the dimension is put back to its original choice before the next one, so
  options are never combined. Buttons that would add to the cart, buy,
  subscribe, sign in or submit a form are never clicked.
- `apply_facet`: a listing page's filter groups are read (group, option,
  count, target URL). Options that are links are separate URLs, handed to the
  frontier like next-page links. Options that filter in place (checkboxes,
  toggle buttons) are applied one at a time, captured and reset. "Apply",
  "Clear all" and other form buttons are never clicked.

Both run under the explorer's GET-only, no-navigation guard.
"""

from __future__ import annotations

from src.modules.content_processor.explore.deep import deep

__all__ = [
    "FACET_DISCOVER_TEMPLATE",
    "UNCHECK_JS",
    "VARIANT_DISCOVER_TEMPLATE",
    "facet_discover_js",
    "variant_discover_js",
]

# Shared helpers. AWAY is replaced with the selector of regions never explored
# (third-party widgets, site utilities, header and footer).
_HELPERS = """
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const clean = (s) => (s || '').trim().replace(/\\s+/g, ' ');
  const AWAY = '__AWAY__';
  const COMMERCE = new RegExp('add to (cart|bag|basket|trolley)|buy( it)? now|\\\\bbuy\\\\b'
    + '|check ?out|wish ?list|favou?rite|subscribe|notify me|sign ?in|log ?in|register'
    + '|pre-?order|book now|reserve|compare', 'i');
  const body = document.body;
  if (!body) { return { dims: [], refused: [], options: [] }; }
  let next = Number(body.getAttribute('data-cp-shop-next') || '0');
  const idOf = (el) => {
    if (!el.hasAttribute('data-cp-shop')) {
      el.setAttribute('data-cp-shop', 's' + next);
      next += 1;
    }
    return el.getAttribute('data-cp-shop');
  };
  const nameOf = (el) => {
    if (el.labels && el.labels.length) {
      return clean(el.labels[0].innerText || el.labels[0].textContent);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = document.getElementById(by.split(/\\s+/)[0]);
      if (t) { return clean(t.textContent); }
    }
    return clean(el.getAttribute('aria-label') || '');
  };
  const isSubmit = (el) => (el.tagName === 'BUTTON' || el.tagName === 'INPUT')
    && String(el.type).toLowerCase() === 'submit';
  const linkOf = (el) => {
    const a = el.tagName === 'A' ? el : null;
    const href = a ? (a.getAttribute('href') || '') : '';
    return href && !href.startsWith('#') && !href.toLowerCase().startsWith('javascript:')
      ? a.href : '';
  };
  const isDisabled = (el) => el.disabled === true || el.getAttribute('aria-disabled') === 'true'
    || /(^|\\s)(disabled|is-disabled)(\\s|$)/.test(el.className || '');
"""

VARIANT_DISCOVER_TEMPLATE = (
    """
() => {
"""
    + _HELPERS
    + """
  const PRODUCT = 'form[action*="cart" i],[itemtype*="schema.org/Product" i],[class*="product" i],'
    + '[id*="product" i],[data-product],[data-product-id],[class*="variant" i],'
    + '[class*="swatch" i],[data-variant],[data-option-name]';
  const DIM_NAME = new RegExp('colou?r|size|variant|option|style|material|finish|capacity'
    + '|storage|memory|width|length|fit|flavou?r|scent|pattern|edition|model|pack|format'
    + '|metal|shade|weight|volume', 'i');
  const NOT_VARIANT = new RegExp('quantity|\\\\bqty\\\\b|sort|order ?by|currency|country'
    + '|language|locale|region|shipping|delivery|per ?page|results|subscription|frequency', 'i');
  const dims = [];
  const refused = [];
  const trim = (s) => clean(s).replace(/[:*]\\s*$/, '').slice(0, 40);

  // 1. Dropdowns: each <select> is one dimension.
  for (const sel of document.querySelectorAll('select')) {
    if (sel.multiple || sel.closest(AWAY)) { continue; }
    const shown = nameOf(sel);
    const hint = [shown, sel.name, sel.id, sel.getAttribute('data-option-name')].join(' ');
    if (NOT_VARIANT.test(hint)) { continue; }
    if (!sel.closest(PRODUCT) && !DIM_NAME.test(hint)) { continue; }
    const options = [];
    Array.from(sel.options).forEach((o, i) => {
      if (!clean(o.text) || o.value === '') { return; }  // "Choose a colour" placeholder
      options.push({ key: String(i), index: i, label: clean(o.text).slice(0, 60),
                     selected: o.selected, disabled: o.disabled, submit: false, href: '',
                     visible: true });
    });
    if (options.length < 2) { continue; }
    dims.push({ id: idOf(sel), mode: 'select', visible: isVisible(sel), options,
                name: trim(shown || sel.getAttribute('data-option-name') || sel.name || sel.id)
                  || 'Option' });
  }

  // 2. Button, swatch and radio groups: options grouped by their container.
  const OPTION = 'input[type=radio],[role=radio],button[aria-pressed],[data-variant],'
    + '[data-variant-id],[data-option-value],[class*="swatch" i] button,[class*="swatch" i] a';
  const GROUP = '[role=radiogroup],fieldset,[data-option-name],[data-option-index]';
  const groups = new Map();
  for (const el of document.querySelectorAll(OPTION)) {
    if (el.closest(AWAY)) { continue; }
    if (!el.closest(PRODUCT) && !el.closest('[role=radiogroup]')) { continue; }
    const label = clean(el.tagName === 'INPUT' ? (nameOf(el) || el.value)
      : (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText
         || el.getAttribute('data-value') || el.getAttribute('data-option-value')
         || (el.querySelector('img') ? el.querySelector('img').alt : ''))).slice(0, 60);
    if (COMMERCE.test(label)) {
      refused.push({ id: idOf(el), label, reason: 'commerce_action_never_clicked' });
      continue;
    }
    const box = el.closest(GROUP) || el.parentElement;
    const key = box ? box : el;
    if (!groups.has(key)) {
      groups.set(key, { box, radioName: el.tagName === 'INPUT' ? el.name : '', members: [] });
    }
    groups.get(key).members.push({ el, label });
  }
  for (const g of groups.values()) {
    if (g.members.length < 2) { continue; }
    const box = g.box;
    let name = '';
    if (box) {
      const legend = box.tagName === 'FIELDSET' ? box.querySelector('legend') : null;
      const prev = box.previousElementSibling;
      name = (legend ? clean(legend.innerText) : '')
        || clean(box.getAttribute('data-option-name') || '')
        || clean(box.getAttribute('aria-label') || '')
        || (prev && clean(prev.innerText).length <= 40 ? clean(prev.innerText) : '');
    }
    name = trim(name || g.radioName || 'Option') || 'Option';
    if (NOT_VARIANT.test(name)) { continue; }
    const options = g.members.map(({ el, label }) => {
      // A visually hidden radio is chosen through its label.
      let target = el;
      if (el.tagName === 'INPUT' && !isVisible(el) && el.labels && el.labels.length) {
        target = el.labels[0];
      }
      const selected = el.checked === true || el.getAttribute('aria-checked') === 'true'
        || el.getAttribute('aria-pressed') === 'true'
        || el.getAttribute('aria-selected') === 'true'
        || /(^|\\s)(selected|active|is-active|is-selected|current)(\\s|$)/
          .test(el.className || '');
      return { key: idOf(target), label: label || 'option', selected,
               disabled: isDisabled(el), submit: isSubmit(el), href: linkOf(el),
               visible: isVisible(target) };
    });
    dims.push({ id: options[0].key, mode: 'click', visible: true, name, options });
  }
  body.setAttribute('data-cp-shop-next', String(next));
  return { dims, refused, options: [] };
}
"""
)

FACET_DISCOVER_TEMPLATE = (
    """
() => {
"""
    + _HELPERS
    + """
  const BOX = '[class*="facet" i],[id*="facet" i],[data-facet],[class*="filter" i],'
    + '[id*="filter" i],[aria-label*="filter" i],[data-filter],[class*="refine" i],'
    + '[id*="refine" i]';
  const FORM_CONTROL = new RegExp('^(clear|reset|remove|apply|done|update|go|search|submit'
    + '|close|cancel|(show|see|view)( \\\\d+)? (results|items|products)|(show|hide) filters?'
    + '|filters?|sort( by)?)\\\\b', 'i');
  const COUNT = /\\(\\s*(\\d[\\d,.]*)\\s*\\)\\s*$/;
  const containers = Array.from(document.querySelectorAll(BOX)).filter((c) => {
    if (c === body || c.tagName === 'HTML' || c.tagName === 'MAIN' || c.closest(AWAY)) {
      return false;
    }
    if (c.getAttribute('role') === 'main' || c.querySelector('h1')) { return false; }
    return !(c.parentElement && c.parentElement.closest(BOX));
  });
  const groupOf = (el, box) => {
    for (let a = el.parentElement; a && a !== box.parentElement; a = a.parentElement) {
      const head = a.querySelector(':scope > legend, :scope > summary, :scope > h2, :scope > h3,'
        + ':scope > h4, :scope > h5, :scope > h6, :scope > [class*="title" i],'
        + ':scope > [class*="heading" i]');
      const named = clean(a.getAttribute('data-facet-name') || a.getAttribute('aria-label')
        || (head && !head.contains(el) ? head.innerText : ''));
      if (named && a !== box) { return named.slice(0, 40); }
    }
    return 'Filter';
  };
  const options = [];
  const OPTION = 'a[href],input[type=checkbox],input[type=radio],[role=checkbox],[role=radio],'
    + 'button,[role=button],input[type=submit],input[type=button]';
  for (const box of containers) {
    const found = Array.from(box.querySelectorAll(OPTION)).filter((el) =>
      !el.hasAttribute('data-cp-ctl') && !el.hasAttribute('aria-expanded')
      && !(el.tagName !== 'A' && el.closest('a[href]')) && el.tagName !== 'SUMMARY');
    if (found.length < 2) { continue; }
    for (const el of found) {
      const isInput = el.tagName === 'INPUT';
      let label = clean(isInput ? (nameOf(el) || el.value)
        : (el.getAttribute('aria-label') || el.innerText || el.textContent));
      const countEl = isInput && el.labels && el.labels.length
        ? el.labels[0].querySelector('[class*="count" i]')
        : el.querySelector('[class*="count" i]');
      let count = countEl ? clean(countEl.textContent).replace(/[^\\d]/g, '') : '';
      const m = label.match(COUNT);
      if (m) {
        count = count || m[1].replace(/[^\\d]/g, '');
        label = clean(label.slice(0, m.index));
      }
      else if (countEl) { label = clean(label.replace(clean(countEl.textContent), '')); }
      const type = String(el.type || el.getAttribute('role') || '').toLowerCase();
      const url = linkOf(el);
      let mode = 'in_page';
      if (COMMERCE.test(label)) { mode = 'commerce'; }
      else if (url) { mode = 'link'; }
      else if (el.tagName === 'A') { mode = 'in_page'; }
      else if (FORM_CONTROL.test(label)) { mode = 'form_control'; }
      else if (isSubmit(el)) { mode = 'submit'; }
      const checked = el.checked === true || el.getAttribute('aria-checked') === 'true'
        || el.getAttribute('aria-pressed') === 'true';
      options.push({ id: idOf(el), group: groupOf(el, box), label: label.slice(0, 60) || type,
                     count: count ? Number(count) : null, url, mode, type, checked,
                     name: el.getAttribute('name') || '',
                     visible: isVisible(el), disabled: isDisabled(el) });
    }
  }
  body.setAttribute('data-cp-shop-next', String(next));
  return { dims: [], refused: [], options };
}
"""
)

# Undo a radio facet when no option of its group was chosen to begin with.
UNCHECK_JS = deep(
    """
(id) => {
  const el = document.querySelector(`[data-cp-shop="${id}"]`);
  if (!el) { return false; }
  el.checked = false;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
"""
)


def variant_discover_js(away: str) -> str:
    """The variant discovery script, never looking inside `away` regions."""
    return deep(VARIANT_DISCOVER_TEMPLATE.replace("__AWAY__", away))


def facet_discover_js(away: str) -> str:
    """The facet discovery script, never looking inside `away` regions."""
    return deep(FACET_DISCOVER_TEMPLATE.replace("__AWAY__", away))
