/**
 * The single source of every user-facing label, colour, explanation and
 * "what to do" line. Engine names (NEEDS_REVIEW, UNVISITED_STATE, ...) are
 * keys here and never appear as primary text anywhere else.
 */

import type { Badge, Block, Finding, GapOut, PageRow } from './api/types';

// ---------------------------------------------------------------------------
// App-wide wording
// ---------------------------------------------------------------------------

export const APP = {
  name: 'Content Capture Results',
  shortName: 'Capture Results',
  nav: {
    runs: 'All runs',
    overview: 'Overview',
    pages: 'Pages',
    duplicates: 'Duplicate content',
    contradictions: 'Contradictions',
    review: 'Needs your review',
    downloads: 'Downloads',
  },
  runPicker: 'Run',
  runPickerPlaceholder: 'Choose a run',
  settings: 'Display settings',
  theme: 'Theme',
  themeLight: 'Light',
  toLight: 'Switch to light theme',
  toDark: 'Switch to dark theme',
  themeDark: 'Dark',
  techNames: 'Show technical names',
  techNamesHelp: 'Adds the engine names next to the plain ones, for engineers.',
  density: 'Table density',
  densityComfortable: 'Comfortable',
  densityCompact: 'Compact',
  columns: 'Columns',
  retry: 'Try again',
  backToRuns: 'Back to all runs',
  notFoundTitle: 'Page not found',
  notFoundText: 'This address does not match any screen.',
  crashTitle: 'This screen ran into a problem',
  crashText: 'Something unexpected happened while showing this screen. Reloading usually fixes it.',
  reload: 'Reload',
  loadErrorTitle: "We couldn't load this",
  copied: 'Copied',
  copyFailed: 'Copying is not allowed in this browser',
  openOriginal: 'Open the original page in a new tab',
  copyUrl: 'Copy address',
  savedReport: 'From the saved report',
  computedReport: 'Worked out just now from the captured pages',
  staleReport:
    'The saved report is older than the captured pages, so some numbers may be out of date. Re-run the report command to refresh it.',
} as const;

// ---------------------------------------------------------------------------
// Capture status (URL badge)
// ---------------------------------------------------------------------------

export interface BadgeInfo {
  label: string;
  /** antd Tag colour preset */
  tag: string;
  /** chart colour */
  color: string;
  meaning: string;
}

export const BADGE_ORDER: Badge[] = [
  'VERIFIED_WITHIN_SCOPE',
  'PARTIAL',
  'NEEDS_REVIEW',
  'BLOCKED',
  'FAILED',
  'EXCLUDED',
  'RUNNING',
];

export const BADGES: Record<Badge, BadgeInfo> = {
  VERIFIED_WITHIN_SCOPE: {
    label: 'Fully captured',
    tag: 'success',
    color: '#389e0d',
    meaning:
      'Everything we were allowed to open on this page was captured and checked against the live page.',
  },
  PARTIAL: {
    label: 'Partly captured',
    tag: 'gold',
    color: '#d48806',
    meaning: 'We got some of the page; part of it could not be opened or read. See Issues.',
  },
  NEEDS_REVIEW: {
    label: 'Needs a check',
    tag: 'blue',
    color: '#1677ff',
    meaning: 'We captured the page but something looks unusual; a person should confirm it.',
  },
  BLOCKED: {
    label: 'Site blocked us',
    tag: 'error',
    color: '#cf1322',
    meaning: 'The website refused automated access (bot protection or login).',
  },
  FAILED: {
    label: "Couldn't load",
    tag: 'volcano',
    color: '#d4380d',
    meaning: 'The page did not load after retries (timeout, server error, rate limit).',
  },
  EXCLUDED: {
    label: 'Skipped by rule',
    tag: 'default',
    color: '#8c8c8c',
    meaning:
      'Not captured on purpose: out of scope, robots.txt, or a file type we do not process.',
  },
  RUNNING: {
    label: 'In progress',
    tag: 'processing',
    color: '#13c2c2',
    meaning: 'Still being captured. This view refreshes on its own.',
  },
};

export function badgeInfo(badge: string): BadgeInfo {
  return (
    BADGES[badge as Badge] ?? {
      label: humanize(badge),
      tag: 'default',
      color: '#8c8c8c',
      meaning: 'A status this version of the app does not know yet.',
    }
  );
}

// ---------------------------------------------------------------------------
// Issue types (GapType plus findings derived by the API)
// ---------------------------------------------------------------------------

export interface IssueInfo {
  title: string;
  why: string;
  todo: string;
  /** antd Alert type for the issue card */
  level: 'error' | 'warning' | 'info';
}

export const ISSUES: Record<string, IssueInfo> = {
  ACCESS_BLOCKED: {
    title: 'Blocked by the website',
    why: 'The site stopped us from reading this page or part of it.',
    todo: 'Retry later, or ask the site owner for an allow-listed way in.',
    level: 'error',
  },
  ACQUISITION_FAILED: {
    title: "Page didn't load",
    why: 'The page did not arrive after several tries.',
    todo: 'Retry the run; check the address in a browser.',
    level: 'error',
  },
  UNSUPPORTED_FORMAT: {
    title: "File type we can't read yet",
    why: 'The file was collected, but its text could not be read.',
    todo: 'The file is listed, but its words are missing. Check it by eye if it matters.',
    level: 'warning',
  },
  BUDGET_STOPPED: {
    title: "Stopped at the run's limit",
    why: 'The run reached its time, request or data limit before finishing this page.',
    todo: 'Re-run with a higher limit if this page matters.',
    level: 'warning',
  },
  DISCOVERY_OPEN: {
    title: "Some page sections weren't opened",
    why: 'Not every tab, menu or button on the page was tried.',
    todo: 'Content behind those clicks may be missing. The notes about tabs, menus and buttons on this page say which ones and why.',
    level: 'warning',
  },
  FIDELITY_DISAGREEMENT: {
    title: 'Live page differs from its source',
    why: 'What the browser showed did not match the page code, or numbers changed on the way.',
    todo: 'Compare the text with the live page by eye.',
    level: 'warning',
  },
  LOW_CONFIDENCE: {
    title: 'Captured, but not confirmed',
    why: 'We have the text, but could not confirm it is exactly right.',
    todo: 'Spot-check the text against the live page.',
    level: 'info',
  },
  UNCONFIRMED_TEXT: {
    title: "Some text couldn't be double-checked",
    why: 'Part of the text was captured but could not be matched against what the live page shows.',
    todo: 'Spot-check the parts marked "Not confirmed" in the Content tab.',
    level: 'info',
  },
  UNVISITED_STATE: {
    title: "A tab, menu or button wasn't opened",
    why: 'Content that only appears after a click or hover may be missing.',
    todo: 'Check the listed items on the live page if their content matters.',
    level: 'warning',
  },
  LIMIT_HIT: {
    title: 'Too large, cut off',
    why: 'The page was bigger than the run allows.',
    todo: 'Only the first part was captured. Re-run with a higher limit if needed.',
    level: 'warning',
  },
  POLICY_EXCLUDED: {
    title: 'Skipped by rule',
    why: 'A rule of this run says not to collect this.',
    todo: 'Nothing to do, unless the rule should change.',
    level: 'info',
  },
  SHADOW_BLOCK_SUSPECTED: {
    title: 'May be showing us a different page',
    why: 'The page looks much thinner than similar pages on the site, which can be a bot-detection page.',
    todo: 'Open the page in a browser and compare.',
    level: 'warning',
  },
  DEGRADED_VARIANT: {
    title: 'Looks like a placeholder or reduced page',
    why: 'The content may be a template or a stripped-down version, not the real page.',
    todo: 'Open the page in a browser and confirm what real visitors see.',
    level: 'warning',
  },
  VOLATILE: {
    title: 'Changes every visit',
    why: 'Counters, dates or prices kept moving while we read the page.',
    todo: 'Compare with care; small differences between runs are expected.',
    level: 'info',
  },
  SOFT_REDIRECT_SUSPECTED: {
    title: 'Possibly redirected elsewhere',
    why: 'The page may be a different page than the one requested (for example the home page).',
    todo: 'Check that the address still shows the intended content.',
    level: 'warning',
  },
  SOFT_404_SUSPECTED: {
    title: 'Looks like a "page not found"',
    why: 'The page loaded, but its content reads like an error page.',
    todo: 'Check whether the page still exists; update links if not.',
    level: 'warning',
  },
};

/** Most decisive first: used to pick the headline issue of a page. */
export const ISSUE_ORDER: string[] = [
  'ACCESS_BLOCKED',
  'ACQUISITION_FAILED',
  'POLICY_EXCLUDED',
  'SOFT_404_SUSPECTED',
  'SHADOW_BLOCK_SUSPECTED',
  'DEGRADED_VARIANT',
  'SOFT_REDIRECT_SUSPECTED',
  'UNSUPPORTED_FORMAT',
  'BUDGET_STOPPED',
  'LIMIT_HIT',
  'FIDELITY_DISAGREEMENT',
  'UNVISITED_STATE',
  'DISCOVERY_OPEN',
  'LOW_CONFIDENCE',
  'UNCONFIRMED_TEXT',
  'VOLATILE',
];

export function issueInfo(key: string): IssueInfo {
  return (
    ISSUES[key] ?? {
      title: humanize(key),
      why: 'An issue type this version of the app does not know yet.',
      todo: 'See the technical details.',
      level: 'info',
    }
  );
}

export function headlineIssue(keys: string[]): string | undefined {
  const ranked = [...keys].sort((a, b) => rank(a) - rank(b));
  return ranked[0];
}

function rank(key: string): number {
  const i = ISSUE_ORDER.indexOf(key);
  return i === -1 ? ISSUE_ORDER.length : i;
}

// ---------------------------------------------------------------------------
// Gap reasons: machine text -> sentence
// ---------------------------------------------------------------------------

const CONTROL_KINDS: Record<string, { one: string; many: string }> = {
  disclosure: { one: 'expandable section', many: 'expandable sections' },
  accordion: { one: 'expandable section', many: 'expandable sections' },
  tab: { one: 'tab', many: 'tabs' },
  find_reveal: { one: 'hidden search result', many: 'hidden search results' },
  menu: { one: 'menu', many: 'menus' },
  scroll_surface: { one: 'scrolling area', many: 'scrolling areas' },
  carousel: { one: 'slider', many: 'sliders' },
  hover: { one: 'hover pop-up', many: 'hover pop-ups' },
  keyboard: { one: 'keyboard-only control', many: 'keyboard-only controls' },
  dialog: { one: 'pop-up window', many: 'pop-up windows' },
  consent: { one: 'cookie notice', many: 'cookie notices' },
  load_more: { one: 'button', many: '"Load more" buttons' },
  pagination: { one: 'next-page link', many: 'next-page links' },
  frame: { one: 'embedded frame', many: 'embedded frames' },
  shadow_root: { one: 'embedded component', many: 'embedded components' },
};

const ACTIONS: Record<string, string> = {
  scroll: 'scrolling',
  expand_disclosure: 'opening expandable sections',
  select_tab: 'clicking tabs',
  hover_info: 'hovering',
  open_informational_dialog: 'opening pop-up windows',
  keyboard_traverse: 'keyboard navigation',
  paginate_get: 'following next-page links',
};

const CLICK_CAUSES: Record<string, string> = {
  intercepted: 'something on the page (such as an overlay) was in the way',
  not_visible: 'it was not visible when we tried',
  outside_viewport: 'it was outside the visible area',
  disabled: 'it was disabled',
  detached: 'it disappeared while we tried',
  timeout: 'the page did not respond in time',
  error: 'the browser reported an error',
};

/** One cause of an unvisited control, as a clause after "because" (singular or plural). */
function causeClause(cause: string, many = false): string {
  const it = many ? 'they' : 'it';
  const was = many ? 'were' : 'was';
  if (cause === 'never_visible') {
    return `${it} never appeared on screen (often a copy meant for mobile screens)`;
  }
  if (cause === 'dialog_left_open') return `${it} opened but would not close again`;
  if (cause === 'restore_failed') return 'the page could not be put back as it was afterwards';
  if (cause === 'click_failed') return `clicking ${many ? 'them' : 'it'} did not work`;
  if (cause.startsWith('click_failed:')) {
    const why = cause.slice('click_failed:'.length);
    const detail = CLICK_CAUSES[why] ?? humanize(why).toLowerCase();
    return `clicking ${many ? 'them' : 'it'} did not work: ${detail.replace(/^it (was|disappeared)/, (_m, verb: string) => `${it} ${verb === 'was' ? was : verb}`)}`;
  }
  if (cause.startsWith('unsafe_side_effect')) {
    const method = cause.split(':')[1]?.split(' ')[0];
    return `opening ${many ? 'them' : 'it'} would send data to the site${method ? ` (a ${method} request)` : ''}, and this run only reads`;
  }
  if (cause.startsWith('policy_disallows:')) {
    const action = cause.slice('policy_disallows:'.length);
    return `${it} ${many ? 'need' : 'needs'} ${ACTIONS[action] ?? humanize(action).toLowerCase()}, which is turned off for this run`;
  }
  if (cause.endsWith('budget_exhausted')) {
    return 'the page reached its limit of clicks, scrolls or time first';
  }
  return humanize(cause).toLowerCase();
}

function causes(cause: string, many = false): string {
  return cause
    .split(';')
    .filter(Boolean)
    .map((c) => causeClause(c, many))
    .join(', and ');
}

export interface ReasonText {
  /** One plain sentence for this single gap. */
  sentence: string;
  /** Gaps with the same group key are shown together. */
  group: string;
  /** Sentence for a group of `n` such gaps (labels listed separately). */
  groupSentence: (n: number) => string;
  /** The control's visible label, when the reason names one. */
  label?: string;
}

const URL_IN_REASON = /(https?:\/\/[^\s:]+(?::\d+)?[^\s:]*)/;

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function single(sentence: string, group: string): ReasonText {
  return { sentence, group, groupSentence: (n) => (n > 1 ? `${sentence} (${n} times)` : sentence) };
}

const VENDORS: Record<string, string> = {
  cloudflare: 'Cloudflare',
  akamai: 'Akamai',
  datadome: 'DataDome',
  perimeterx: 'HUMAN (PerimeterX)',
  imperva: 'Imperva',
  kasada: 'Kasada',
  generic: '',
  unknown: '',
};

const BLOCK_KINDS: Record<string, (vendor: string) => string> = {
  js_challenge: (v) => `The site's bot protection${v} asked for a browser check we could not pass.`,
  interactive_captcha: (v) => `The site's bot protection${v} showed a CAPTCHA puzzle.`,
  fingerprint: (v) => `The site's bot protection${v} recognised us as an automated tool and refused the page.`,
  ip_reputation: (v) => `The site's bot protection${v} refused requests from our network address.`,
  rate: () => 'The site asked us to slow down (too many requests).',
  auth_wall: () => 'The page requires signing in.',
  paywall: () => 'The page is behind a paywall.',
  geo_gate: () => 'The page is not available in the region we visited from.',
  legal: () => 'The page is unavailable for legal reasons.',
  pay_per_crawl: () => 'The site charges automated visitors per page (pay-per-crawl), so we did not continue.',
  ai_optout: () => 'The site asks AI tools not to use its content, so we did not collect it.',
  shadow_block_suspected: () => 'The page we received looks like a bot-detection page rather than the real one.',
  soft_404: () => 'The page loaded, but its content reads like a "page not found" message.',
  wrong_payload: () => 'The site sent something other than the page we asked for.',
  transport: () => 'The connection to the site failed.',
};

/** Turn a gap's machine reason into a plain sentence. Unknown patterns fall back to the issue title. */
export function describeReason(gapType: string, reason: string): ReasonText {
  const r = reason.trim();

  // unvisited_<kind>:<cause>:<label>   (cause may hold "METHOD url" or "a;b")
  const unvisited = /^unvisited_([a-z_]+):(.*)$/.exec(r);
  if (unvisited) {
    const kindKey = unvisited[1] ?? '';
    let rest = unvisited[2] ?? '';
    const kind = CONTROL_KINDS[kindKey] ?? { one: humanize(kindKey).toLowerCase(), many: humanize(kindKey).toLowerCase() };
    let cause: string;
    let label = '';
    const unsafe = /^unsafe_side_effect:(\w+) /.exec(rest);
    if (unsafe) {
      const afterMethod = rest.slice(unsafe[0].length);
      const url = URL_IN_REASON.exec(afterMethod);
      const tail = url ? afterMethod.slice((url.index ?? 0) + url[0].length) : afterMethod;
      cause = `unsafe_side_effect:${unsafe[1] ?? ''}`;
      label = tail.startsWith(':') ? tail.slice(1) : tail;
      rest = '';
    } else {
      const twoPart = /^(policy_disallows|click_failed):([a-z_]+)(?::(.*))?$/.exec(rest);
      if (twoPart && (twoPart[1] === 'policy_disallows' || CLICK_CAUSES[twoPart[2] ?? ''])) {
        cause = `${twoPart[1]}:${twoPart[2]}`;
        label = twoPart[3] ?? '';
      } else {
        const idx = rest.indexOf(':');
        cause = idx === -1 ? rest : rest.slice(0, idx);
        label = idx === -1 ? '' : rest.slice(idx + 1);
      }
    }
    label = label.trim();
    const because = causes(cause);
    const named = label ? `The "${label}" ${kind.one}` : `A ${kind.one}`;
    const stuckOpen = cause === 'dialog_left_open';
    const sentence = kindKey === 'scroll_surface'
      ? `We stopped scrolling ${label === 'page' ? 'the page' : label ? `"${label}"` : 'an area'} before reaching the end, because ${because}.`
      : stuckOpen
        ? `${named} opened but would not close again, so its content was not kept.`
        : `${named} wasn't opened because ${because}.`;
    return {
      sentence,
      group: `unvisited:${kindKey}:${cause}`,
      groupSentence: (n) =>
        n === 1 || kindKey === 'scroll_surface'
          ? sentence
          : stuckOpen
            ? `${n} ${kind.many} opened but would not close again, so their content was not kept.`
            : `${n} ${kind.many} weren't opened because ${causes(cause, true)}.`,
      label: label || undefined,
    };
  }

  // discovery_open:unvisited_controls=N[:remaining_controls=M] | :not_rendered | :not_explored | :explore_error
  if (r.startsWith('discovery_open')) {
    const n = /unvisited_controls=(\d+)/.exec(r)?.[1];
    if (n) {
      return single(
        `${n} clickable ${Number(n) === 1 ? 'part' : 'parts'} of the page (tabs, menus, buttons) ${Number(n) === 1 ? 'was' : 'were'} not opened, so some hidden content may be missing.`,
        'discovery:unvisited',
      );
    }
    if (r.includes('not_rendered')) {
      return single(
        "The page wasn't opened in a real browser, so we couldn't look for tabs, menus or buttons.",
        'discovery:not_rendered',
      );
    }
    if (r.includes('explore_error')) {
      return single('Trying the page\'s tabs, menus and buttons failed part-way.', 'discovery:error');
    }
    if (r.includes('not_explored')) {
      return single("We didn't try the page's tabs, menus or buttons.", 'discovery:not_explored');
    }
    return single('Not every part of the page could be explored.', 'discovery:other');
  }

  if (/ocr_engine_missing|image_extracted;ocr|r8:ocr_unavailable|visual_text_unextracted/.test(r)) {
    return single(
      'Reading words inside images needs a text-recognition (OCR) tool that is not installed, so any text in the image was not captured.',
      'ocr_missing',
    );
  }
  if (r.startsWith('ocr_error') || r.startsWith('ocr_execution_error') || r.startsWith('region_ocr_error')) {
    return single('Reading the text in the image failed.', 'ocr_error');
  }
  if (r.startsWith('ocr_confidence')) {
    return single('Text was read from the image, but the reading is unsure.', 'ocr_confidence');
  }
  if (r.startsWith('ocr_lang')) {
    return single('The image text is in a language the text-recognition tool is not set up for.', 'ocr_lang');
  }
  if (r.startsWith('placeholder_content_suspected') || r.startsWith('placeholder_suspected')) {
    return single(
      'The page shows placeholder text (such as "Lorem ipsum"), so it may be a template rather than real content.',
      'placeholder',
    );
  }
  if (r.startsWith('product_unavailable')) {
    return single('The page says the product is unavailable.', 'product_unavailable');
  }
  if (r.startsWith('mime_not_allowed')) {
    const mime = r.split(':')[1] ?? '';
    return single(
      mime === 'application/x-empty'
        ? 'The address returned an empty response (usually a tracking pixel), so there was nothing to collect.'
        : `This file type (${mime || 'unknown'}) is not one this run collects.`,
      `mime:${mime}`,
    );
  }
  if (r.startsWith('robots_disallow')) {
    return single(
      "The site's robots.txt asks automated tools not to visit this address, and this run respects it.",
      'robots',
    );
  }
  if (r.startsWith('age_or_audience_gate')) {
    return single(
      'An age or audience check (for example "Are you a financial professional?") covers the main content.',
      'gate:audience',
    );
  }
  if (r.startsWith('lead_gen_form_wall')) {
    return single('A sign-up form has to be filled in before the main content is shown.', 'gate:form');
  }
  if (r.startsWith('login_wall')) {
    return single('The main content is behind a login.', 'gate:login');
  }
  if (r.startsWith('soft_paywall')) {
    return single('A subscription or article-limit overlay covers the content.', 'gate:paywall');
  }
  if (r.startsWith('geo_pincode_modal')) {
    return single('The page asks for a location or postcode before showing its content.', 'gate:geo');
  }
  if (r.startsWith('soft_404')) {
    return single('The page loaded, but its content reads like a "page not found" message.', 'soft404');
  }
  if (r.startsWith('truncated_body') || r.startsWith('limit_hit')) {
    return single('The page was larger than the size limit, so only the first part was captured.', 'truncated');
  }
  if (r.startsWith('rate_limited')) {
    return single('The site asked us to slow down (too many requests), and the page was not loaded.', 'rate');
  }
  if (r.startsWith('frame_not_acquired:') || r.startsWith('frame_failed:') || r.startsWith('frame_blocked:')) {
    const url = r.slice(r.indexOf(':') + 1);
    return single(`An embedded frame from ${hostOf(url)} could not be loaded.`, `frame:${hostOf(url)}`);
  }
  if (r.startsWith('critical_tokens_changed')) {
    const lost = /lost=([^;]*)/.exec(r)?.[1];
    const added = /added=([^;]*)/.exec(r)?.[1];
    const parts = [lost ? `missing: ${lost}` : '', added ? `extra: ${added}` : ''].filter(Boolean).join('; ');
    return single(
      `Numbers or symbols differ between the page and our copy${parts ? ` (${parts})` : ''}.`,
      'critical_tokens',
    );
  }
  if (r.startsWith('obfuscated_font_suspected')) {
    return single('The page uses a scrambled font, so the text we read may not match what people see.', 'font');
  }
  if (r.startsWith('budget_exhausted') || r.endsWith('_budget_exhausted')) {
    return single('The run reached its limit (time, requests or data) before this page was finished.', 'budget');
  }
  if (r.startsWith('poison:')) {
    return single('Processing this page failed repeatedly, so it was stopped.', 'poison');
  }
  if (r.startsWith('evidence_missing')) {
    return single('Some of the saved proof for this page is missing, so it could not be re-checked.', 'evidence');
  }
  if (r.startsWith('settle_window_exhausted')) {
    return single('The page kept changing while we read it.', 'settle');
  }
  if (r.startsWith('render_failed') || r.startsWith('render:')) {
    return single('Opening the page in a real browser failed.', 'render_failed');
  }
  if (r.startsWith('transport:')) {
    return single('The connection to the site failed.', 'transport');
  }
  if (r.startsWith('extraction_timeout') || r.startsWith('extraction_failed') || r.startsWith('pdf_open_error') || r.startsWith('corrupt_image_payload')) {
    return single('The file could not be opened or read.', 'file_unreadable');
  }
  if (r === 'scheduled_not_started') {
    return single('The page was planned but the run ended before it started.', 'not_started');
  }
  if (r === 'excluded_at_plan') {
    return single('The page was left out when the run was planned.', 'excluded_plan');
  }

  // Block classifier reasons: <kind>:<vendor>:fetched_at=...:host_tier=...
  const block = /^([a-z_]+):([a-z0-9_-]+)/.exec(r);
  const blockKind = block?.[1] ?? r.split(':')[0] ?? '';
  const describe = BLOCK_KINDS[blockKind];
  if (describe) {
    const vendorKey = block?.[2] ?? '';
    const vendor = VENDORS[vendorKey] ?? (vendorKey ? humanize(vendorKey) : '');
    return single(describe(vendor ? ` (${vendor})` : ''), `block:${blockKind}`);
  }

  const info = issueInfo(gapType);
  return single(`${info.why}`, `other:${gapType}`);
}

export interface ReasonGroup {
  key: string;
  gapType: string;
  sentence: string;
  labels: string[];
  gaps: GapOut[];
  retryable: boolean;
}

/** Group a page's gaps by issue type and cause, most decisive issue first. */
export function groupGaps(gaps: GapOut[]): ReasonGroup[] {
  const groups = new Map<string, { gapType: string; text: ReasonText; labels: string[]; gaps: GapOut[] }>();
  for (const gap of gaps) {
    const text = describeReason(gap.gap_type, gap.reason);
    const key = `${gap.gap_type}|${text.group}`;
    const existing = groups.get(key);
    if (existing) {
      existing.gaps.push(gap);
      if (text.label && !existing.labels.includes(text.label)) existing.labels.push(text.label);
    } else {
      groups.set(key, { gapType: gap.gap_type, text, labels: text.label ? [text.label] : [], gaps: [gap] });
    }
  }
  return [...groups.entries()]
    .map(([key, g]) => ({
      key,
      gapType: g.gapType,
      sentence: g.gaps.length === 1 ? g.text.sentence : g.text.groupSentence(g.gaps.length),
      labels: g.labels,
      gaps: g.gaps,
      retryable: g.gaps.some((x) => x.retry_eligible),
    }))
    .sort((a, b) => rank(a.gapType) - rank(b.gapType) || b.gaps.length - a.gaps.length);
}

export function describeFinding(f: Finding): string {
  if (f.key === 'UNCONFIRMED_TEXT') {
    return f.total > 0
      ? `${f.unconfirmed} of ${f.total} pieces of text could not be double-checked against what the live page shows. They are usually parts that only appear after a click, such as tabs or pop-ups.`
      : 'Some text could not be double-checked against what the live page shows.';
  }
  return issueInfo(f.key).why;
}

// ---------------------------------------------------------------------------
// Warnings on a record
// ---------------------------------------------------------------------------

export function describeWarning(w: string): string {
  if (w === 'frames_from_source_dom_only') {
    return 'Embedded frames were read from the page code only, not from the live page.';
  }
  if (w.startsWith('render_not_run')) return 'The page was not opened in a real browser.';
  if (w.startsWith('status_lowered_by_gaps')) return 'The status was lowered because of the issues found.';
  if (w.startsWith('r6:')) return 'Text inside screenshots was not checked (no text-recognition tool).';
  if (w.startsWith('r8:')) return 'Images inside PDFs were not read (no text-recognition tool).';
  return humanize(w);
}

// ---------------------------------------------------------------------------
// Where content was found
// ---------------------------------------------------------------------------

export const LAYERS: Record<string, string> = {
  rendered_visible: 'Visible on the page',
  collapsed_published: 'Behind a click (FAQ, accordion, tab)',
  hidden: 'Hidden on the page',
  accessibility_only: 'For screen readers only',
  metadata: 'Page metadata (title, description)',
  source_only: 'In the page code, not shown',
  third_party_chrome: 'Third-party widget',
  ugc: 'User comments and reviews',
  icon_artifact: 'Icon text',
  paywalled_in_source: 'Behind a paywall',
  uncertain: 'Unclear',
  hidden_revision: 'Earlier version (tracked change)',
  design_evidence: 'Design reference',
};

/** Layers shown by default; the rest appear with "Include hidden text". */
export const VISIBLE_LAYERS = new Set(['rendered_visible', 'collapsed_published']);

export const REGIONS: Record<string, string> = {
  main_content: 'Main content',
  header: 'Header',
  hero: 'Top banner',
  nav_menu: 'Navigation menu',
  hamburger_menu: 'Mobile menu',
  mega_menu: 'Mega menu',
  breadcrumb: 'Breadcrumb',
  sidebar: 'Sidebar',
  footer: 'Footer',
  modal: 'Pop-up',
  popover: 'Pop-over',
  tooltip: 'Tooltip',
  accordion_panel: 'FAQ or accordion',
  tab_panel: 'Tab',
  carousel: 'Slider',
  card: 'Card',
  snippet: 'Snippet',
  palette: 'Colour palette',
  search: 'Search',
  form: 'Form',
  table: 'Table',
  cookie_banner: 'Cookie notice',
  chat_widget: 'Chat widget',
  ad_slot: 'Ad',
  document_metadata: 'Document properties',
  page_metadata: 'Page metadata',
  pdf_page: 'PDF page',
  pdf_table: 'PDF table',
  pdf_form: 'PDF form',
  pdf_annotation: 'PDF comment',
  image_text: 'Text in an image',
  unknown: 'Other',
};

export type AreaGroup = 'main' | 'navigation' | 'footer' | 'overlays' | 'metadata';

export const AREA_GROUPS: Record<AreaGroup, { label: string; help: string }> = {
  main: { label: 'Main content', help: 'The body of the page: text, tables, FAQs, tabs and cards.' },
  navigation: { label: 'Header and menus', help: 'Site header, navigation and mobile menus, breadcrumbs, search.' },
  footer: { label: 'Footer', help: 'Links and legal text at the bottom of every page.' },
  overlays: { label: 'Pop-ups and widgets', help: 'Pop-ups, tooltips, cookie notices, chat widgets and ads.' },
  metadata: { label: 'Page metadata', help: 'Title, description and document properties.' },
};

export const AREA_GROUP_ORDER: AreaGroup[] = ['main', 'navigation', 'footer', 'overlays', 'metadata'];
export const DEFAULT_AREA_GROUPS: AreaGroup[] = ['main'];

const REGION_GROUP: Record<string, AreaGroup> = {
  header: 'navigation',
  nav_menu: 'navigation',
  hamburger_menu: 'navigation',
  mega_menu: 'navigation',
  breadcrumb: 'navigation',
  search: 'navigation',
  footer: 'footer',
  modal: 'overlays',
  popover: 'overlays',
  tooltip: 'overlays',
  cookie_banner: 'overlays',
  chat_widget: 'overlays',
  ad_slot: 'overlays',
  page_metadata: 'metadata',
  document_metadata: 'metadata',
};

const ROLE_GROUP: Record<string, AreaGroup> = {
  nav: 'navigation',
  chrome: 'navigation',
  footer: 'footer',
};

/** Which filter group a block belongs to: its page area, else its role. */
export function areaGroup(block: Pick<Block, 'region' | 'role' | 'layer'>): AreaGroup {
  if (block.layer === 'metadata') return 'metadata';
  if (block.region) return REGION_GROUP[block.region] ?? 'main';
  return ROLE_GROUP[block.role] ?? 'main';
}

export function regionLabel(region: string | null): string {
  return region ? (REGIONS[region] ?? humanize(region)) : 'Not identified';
}

export const FORMATS: Record<string, string> = {
  html: 'Web page',
  pdf: 'PDF',
  image: 'Image',
  office_doc: 'Word document',
  spreadsheet: 'Spreadsheet',
  presentation: 'Slides',
  text: 'Text file',
  json: 'Text file',
  xml: 'Text file',
  audio: 'Audio',
  video: 'Video',
  other: 'Other',
};

export function formatLabel(format: string | null | undefined): string {
  if (!format) return 'Unknown';
  return FORMATS[format] ?? humanize(format);
}

export const TIERS: Record<string, string> = {
  T0_AUTHORIZED: 'Authorised access',
  T1_POLITE_HTTP: 'Standard request',
  T2_IMPERSONATED_HTTP: 'Browser-like request',
  T3_FIDELITY_BROWSER: 'Real browser',
  T4_PROXIED: 'Proxy (paid)',
  T5_MANAGED_UNBLOCKER: 'Proxy (paid)',
  T6_POLICY_GATED: 'Special approval (paid)',
};

export function tierLabel(tier: string | null | undefined): string {
  if (!tier) return 'Not recorded';
  return TIERS[tier] ?? humanize(tier);
}

export const CHANGES: Record<string, { label: string; tag: string }> = {
  new: { label: 'First capture', tag: 'cyan' },
  changed: { label: 'Changed since last run', tag: 'orange' },
  unchanged: { label: 'Unchanged (reused)', tag: 'default' },
  unknown: { label: 'Unknown', tag: 'default' },
};

export function changeLabel(change: string | null | undefined): string {
  if (!change) return 'Not compared';
  return CHANGES[change]?.label ?? humanize(change);
}

export const DATE_SOURCES: Record<string, string> = {
  'http:last-modified': "the website's server (Last-Modified header)",
  jsonld: "the page's structured data",
  meta: "the page's meta tags",
  pdf: "the PDF's properties",
  office: "the document's properties",
  exif: "the image's properties",
};

export function dateSourceLabel(source: string | null | undefined): string {
  if (!source) return '';
  return DATE_SOURCES[source] ?? DATE_SOURCES[source.split(':')[0] ?? ''] ?? humanize(source);
}

export const LINK_KINDS: Record<string, string> = {
  embeds: 'Embedded file',
  frames: 'Embedded frame',
  links_to: 'Linked download',
};

export const STOP_REASONS: Record<string, string> = {
  drained: 'Finished normally',
  budget_exhausted: "Stopped at the run's limit",
  cancelled: 'Cancelled',
};

// ---------------------------------------------------------------------------
// Pages table
// ---------------------------------------------------------------------------

export const PAGES = {
  title: 'Pages',
  subtitle: 'Every address in this run, with what we captured and what is missing.',
  allStatuses: 'All',
  site: 'Website',
  sitePlaceholder: 'All websites',
  type: 'Content type',
  typePlaceholder: 'All types',
  issue: 'Issue',
  issuePlaceholder: 'Any issue',
  onlyIssues: 'Only pages with issues',
  search: 'Search addresses and page text',
  clearFilters: 'Clear filters',
  columns: 'Columns',
  noIssues: 'None',
  issuesTotal: (n: number) => `${n} issues in total`,
  range: (from: number, to: number, total: number) => `${from}–${to} of ${total.toLocaleString()} ${total === 1 ? 'page' : 'pages'}`,
  emptyFiltered: 'No pages match these filters. Try clearing a filter or searching for something shorter.',
  emptyRun: 'This run has no pages yet.',
  col: {
    page: 'Page',
    status: 'Capture status',
    words: 'Words',
    issues: 'Issues',
    type: 'Content type',
    modified: 'Site last updated',
    captured: 'Captured on',
    change: 'Change',
  },
  colHelp: {
    words: 'Words of visible text we captured (menus and footer included).',
    issues: 'Things that may be missing or need a check on this page.',
    modified: 'When the website says the page was last updated.',
  },
  total: (n: number) => `${n.toLocaleString()} ${n === 1 ? 'page' : 'pages'}`,
} as const;

// ---------------------------------------------------------------------------
// Page detail
// ---------------------------------------------------------------------------

export const DETAIL = {
  tabs: {
    content: 'Content',
    issues: 'Issues',
    facts: 'Page facts',
    technical: 'Technical',
  },
  show: 'Show',
  includeHidden: 'Include hidden text',
  includeHiddenHelp: 'Text that is in the page but not shown to visitors (screen-reader text, metadata, hidden panels).',
  onlyUnconfirmed: 'Only text not confirmed',
  searchPage: 'Search within this page',
  copyVisible: 'Copy shown text',
  verified: 'Matched on the live page',
  notVerified: 'Not confirmed on the live page',
  notVerifiedShort: 'Not confirmed',
  repeated: (n: number) => `${n} repeated ${n === 1 ? 'piece' : 'pieces'} of text (the same menu or link twice, for example) are left out.`,
  shownCount: (shown: number, total: number) => `Showing ${shown.toLocaleString()} of ${total.toLocaleString()} pieces of text`,
  noBlocks: 'We have no text for this page.',
  noBlocksFiltered: 'No text matches these settings. Turn on more page areas above, or include hidden text.',
  noIssues: 'Nothing is missing from this page. Everything we were allowed to open was captured and checked.',
  canRetry: 'Can retry',
  canRetryHelp: 'Running the capture again may fix this.',
  technicalDetails: 'Technical details',
  whatToDo: 'What to do',
  items: 'Items',
  showFewer: 'Show fewer',
  showAll: (n: number) => `Show all ${n}`,
  stats: { words: 'Words', pieces: 'Pieces of text', issues: 'Issues' },
  untitled: '(no heading)',
  copySuccess: 'Shown text copied',
  facts: {
    requested: 'Address we asked for',
    final: 'Address we ended up on',
    sameAddress: 'Same as the address we asked for',
    aliases: 'Other addresses for this page',
    captured: 'Captured on',
    modified: 'Site last updated',
    modifiedSource: (s: string) => `according to ${s}`,
    published: 'First published',
    change: 'Change since last run',
    changeWhy: 'Why',
    previousRun: 'Compared with run',
    reused: 'Text reused from run',
    access: 'How we accessed it',
    fileType: 'File type',
    links: 'Files and frames found on this page',
    linksNone: 'None found.',
    inRun: 'Open in this run',
    warnings: 'Notes',
    notRecorded: 'Not recorded',
  },
  tech: {
    hint: 'Engine details for engineers. Turn off "Show technical names" in display settings to hide this tab.',
    statusVector: 'Status vector',
    inventory: 'Inventory counts',
    discovery: 'Discovery',
    warnings: 'Warnings',
    raw: 'Full record (JSON)',
    showRaw: 'Load the full record',
  },
} as const;

/** One plain sentence summarising the outcome of a page. */
export function outcomeSentence(row: PageRow, blockCount: number): string {
  const words = `${row.words.toLocaleString()} ${row.words === 1 ? 'word' : 'words'}`;
  const issues = `${row.issues} ${row.issues === 1 ? 'issue' : 'issues'}`;
  switch (row.badge) {
    case 'VERIFIED_WITHIN_SCOPE':
      return `We captured ${words} from this page and checked all of it against the live page.`;
    case 'PARTIAL':
      return blockCount > 0
        ? `We captured ${words}, but ${issues} mean part of the page may be missing.`
        : `We reached this address but could not read its content (${issues}).`;
    case 'NEEDS_REVIEW':
      return `We captured ${words}. ${issues.charAt(0).toUpperCase() + issues.slice(1)} ${row.issues === 1 ? 'needs' : 'need'} a person to confirm the result.`;
    case 'BLOCKED':
      return 'The website refused our visit, so nothing (or only part) of this page was captured.';
    case 'FAILED':
      return 'The page did not load, even after retries.';
    case 'EXCLUDED':
      return 'This address was skipped on purpose by the rules of this run.';
    case 'RUNNING':
      return 'This page is still being captured. This view refreshes on its own.';
    default:
      return `We captured ${words}.`;
  }
}

// ---------------------------------------------------------------------------
// Run overview and runs list
// ---------------------------------------------------------------------------

export const RUNS = {
  title: 'All runs',
  subtitle: 'Each run is one capture of a set of web pages. Open one to see what was captured.',
  empty:
    'No runs found. Produce one with the dry-run command (it writes a <name>.records.jsonl file), then start this server with --exports pointing at that folder.',
  emptyFolder: (dir: string) => `Looked in: ${dir}`,
  pages: (n: number) => `${n.toLocaleString()} ${n === 1 ? 'page' : 'pages'}`,
  pagesNoun: (n: number) => (n === 1 ? 'page' : 'pages'),
  fully: (pct: number) => `${pct}% fully captured`,
  issues: (n: number) => `${n.toLocaleString()} ${n === 1 ? 'issue' : 'issues'}`,
  open: 'Open run',
  noReports: 'No report files yet',
  running: 'Capture in progress',
  unreadable: (n: number) => `${n} ${n === 1 ? 'line' : 'lines'} of the file could not be read`,
} as const;

export const OVERVIEW = {
  title: 'Run overview',
  totalPages: 'Pages',
  totalPagesHelp: 'Addresses in this run',
  donutTitle: 'Capture status',
  topReasons: "Top reasons pages aren't fully captured",
  topReasonsEmpty: 'Every page was fully captured. Nothing to fix.',
  pagesAffected: (n: number) => `${n.toLocaleString()} ${n === 1 ? 'page' : 'pages'}`,
  retryable: (n: number) => `${n} can be retried`,
  contentFound: 'Content found',
  contentFoundHelp: 'Pages by the kind of file they are.',
  sites: 'Websites',
  access: 'How we accessed the pages',
  changes: 'Changes since last run',
  words: 'Words captured',
  capturedOn: 'Captured',
  duration: 'Run took',
  outcome: 'Run ended',
  seePages: 'See pages',
  pctOfPages: (pct: number) => `${pct}% of pages`,
} as const;

// ---------------------------------------------------------------------------
// Duplicates, contradictions, review, downloads
// ---------------------------------------------------------------------------

export const DUPLICATES = {
  title: 'Duplicate content',
  subtitle:
    'Groups of pages that share most of their main text (menus and footers are ignored). Large groups are often templates or copies.',
  groups: 'Groups of similar pages',
  pairs: 'Page pairs',
  groupTitle: (n: number) => `${n} pages share most of their text`,
  representative: 'Fullest copy',
  representativeHelp: 'The page in this group with the most text; a good one to keep.',
  overlap: 'Text shared with the group',
  overlapHelp: "How much of this page's text also appears on the other pages of the group.",
  sharedBlocks: 'Shared passages',
  similarity: 'Similarity',
  similarityHelp: 'How much of the two pages’ text is the same (100% = identical).',
  aInB: 'Of page A found in B',
  bInA: 'Of page B found in A',
  sharedText: 'Shared text',
  exact: 'Identical text',
  empty: (threshold: number) =>
    `No pages share more than ${threshold}% of their main text. Nothing looks duplicated.`,
  settings: (metric: string, threshold: number) =>
    `Pages are grouped when at least ${threshold}% of their text ${metric === 'containment' ? 'is contained in another page' : 'is the same'}.`,
  viewPair: 'Compare',
  pairTitle: 'Compare two pages',
  pageA: 'Page A',
  pageB: 'Page B',
} as const;

export const CONTRADICTIONS = {
  title: 'Contradictions',
  subtitle:
    'Statements on different pages that look like they disagree, for example two different prices or dates for what seems to be the same thing.',
  empty: 'No statements were found that contradict each other.',
  emptyFiltered: 'No contradictions match this filter.',
  filterLabel: 'How likely',
  all: 'All',
  statementA: 'Statement A',
  statementB: 'Statement B',
  why: 'Why it was flagged',
  context: 'Context compared',
  confidence: 'Confidence in the captured text',
  decision: 'Reviewer decision',
  undecided: 'Not reviewed yet',
  judgeOff: 'Checked with simple rules (numbers, dates, “not”); no language model was used.',
  judgeOn: 'Also checked by a language model.',
  score: 'Contradiction likelihood',
} as const;

export const BUCKETS: Record<string, { label: string; tag: string; help: string; order: number }> = {
  context_matched: {
    label: 'Likely a real conflict',
    tag: 'red',
    help: 'Both statements are about the same thing in the same context, but say different things.',
    order: 0,
  },
  context_unknown: {
    label: 'Worth a look',
    tag: 'gold',
    help: 'We could not tell whether both statements are about the same thing.',
    order: 1,
  },
  context_differs: {
    label: 'Probably fine',
    tag: 'default',
    help: 'The statements look like they are about different things (another product, date or audience).',
    order: 2,
  },
};

export const CONFIDENCE: Record<string, { label: string; tag: string }> = {
  high: { label: 'High', tag: 'green' },
  medium: { label: 'Medium', tag: 'gold' },
  low: { label: 'Low', tag: 'red' },
};

export const DECISIONS: Record<string, string> = {
  confirmed: 'Confirmed conflict',
  not_a_contradiction: 'Not a contradiction',
  context_differs: 'Different context',
};

const CONTEXT_FIELDS: Record<string, string> = {
  entity: 'Subject',
  product_or_variant: 'Product',
  geography: 'Region',
  audience: 'Audience',
  conditions: 'Conditions',
  codes: 'Codes (tickers, plan codes)',
  period: 'Time period',
};

export function describeContextField(name: string, value: string): { field: string; text: string; tag: string } {
  const field = CONTEXT_FIELDS[name] ?? humanize(name);
  if (value === 'match') return { field, text: 'Same', tag: 'green' };
  if (value === 'unknown') return { field, text: 'Unclear', tag: 'default' };
  const differs = /^differs\((.*)\)$/.exec(value);
  if (differs) {
    const [a, b] = (differs[1] ?? '').split('|');
    return { field, text: `Different: ${a ?? ''} vs ${b ?? ''}`, tag: 'orange' };
  }
  return { field, text: value, tag: 'default' };
}

export function describeFlag(flag: string): string {
  const numeric = /^numeric:([^:]+):(.*)!=(.*)$/.exec(flag);
  if (numeric) return `Different amounts: ${numeric[2]} vs ${numeric[3]}`;
  if (flag === 'negation_flip') return 'One statement says "not", the other does not';
  const date = /^date_conflict:(from|to):(.*)!=(.*)$/.exec(flag);
  if (date) return `Different ${date[1] === 'from' ? 'start' : 'end'} dates: ${date[2]} vs ${date[3]}`;
  return humanize(flag);
}

export const REVIEW = {
  title: 'Needs your review',
  subtitle: 'Pages where a person should decide what to do. The most serious are listed first.',
  empty: 'Nothing needs your review. Every page was captured and checked.',
  mainIssue: 'Main issue',
  whatToDo: 'What to do',
  otherIssues: (n: number) => `+${n} more`,
  decision: 'Decision recorded',
  noDecision: 'None yet',
  engineSuggestion: 'Engine suggestion',
  count: (n: number) => `${n.toLocaleString()} ${n === 1 ? 'page' : 'pages'} to review`,
} as const;

export const DOWNLOADS = {
  title: 'Downloads',
  subtitle: 'Spreadsheets and files made from this run, for sharing or further analysis.',
  empty:
    'No report files were written for this run yet. Create them with: python -m src.modules.content_processor.cli report --records <run>.records.jsonl --out <run>_reports',
  download: 'Download',
  size: 'Size',
  updated: 'Updated',
  other: 'Other file from the run',
} as const;

// Report files are named `<run>_<report>` (older runs: just `<report>`).
const FILE_DESCRIPTIONS: { match: RegExp; title: string; text: string }[] = [
  { match: /(?:^|_)project_report\.xlsx$/, title: 'Project report', text: 'One workbook for the whole run: every page with its content, whether it was fully extracted, the share confirmed and the confidence; and, for every page, the other pages that carry the same text and how much.' },
  { match: /(?:^|_)r1_url_content\./, title: 'Page content', text: 'One row per page: its status, word count, issues and all of its captured text.' },
  { match: /(?:^|_)review_queue\./, title: 'Review list', text: 'Pages that need a decision, with empty columns for your action and notes.' },
  { match: /(?:^|_)r2_overlap\./, title: 'Duplicate content (workbook)', text: 'Both duplicate-content sheets: groups of similar pages and page pairs.' },
  { match: /(?:^|_)r2a_overlap_groups\./, title: 'Duplicate content: groups', text: 'Each group of similar pages, one row per page, with how much text it shares.' },
  { match: /(?:^|_)r2b_overlap_pairs\./, title: 'Duplicate content: pairs', text: 'Every pair of similar pages with similarity scores and shared passages.' },
  { match: /(?:^|_)r4_contradictions\./, title: 'Contradictions', text: 'Statements that may disagree, side by side, with a column for your decision.' },
  { match: /(?:^|_)explorer\.html$/, title: 'Offline page viewer', text: 'The older single-file viewer for this run; opens in a browser without this server.' },
  { match: /(?:^|_)manifest\.json$/, title: 'Report summary', text: 'Counts and settings used to make these reports (for engineers).' },
];

const FILE_KINDS: Record<string, string> = {
  xlsx: 'Excel',
  csv: 'CSV',
  jsonl: 'JSON lines',
  json: 'JSON',
  html: 'Web page',
};

export function describeFile(name: string): { title: string; text: string; kind: string } {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  const found = FILE_DESCRIPTIONS.find((d) => d.match.test(name));
  return {
    title: found?.title ?? name,
    text: found?.text ?? DOWNLOADS.other,
    kind: FILE_KINDS[ext] ?? ext.toUpperCase(),
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Fallback for engine names without an entry: `SOME_NAME` -> "Some name". */
export function humanize(value: string): string {
  const text = value.replace(/[_:]+/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : value;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDate(iso: string | null | undefined, withTime = false): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, withTime
    ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '';
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
