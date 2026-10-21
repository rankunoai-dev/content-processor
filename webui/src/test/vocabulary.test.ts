import { describe, expect, it } from 'vitest';
import {
  BADGES,
  BADGE_ORDER,
  ISSUES,
  areaGroup,
  badgeInfo,
  describeContextField,
  describeFile,
  describeFlag,
  describeReason,
  groupGaps,
  headlineIssue,
  issueInfo,
  outcomeSentence,
} from '../vocabulary';
import { row } from './utils';

const ENGINE_TERMS = /\b(NEEDS_REVIEW|VERIFIED_WITHIN_SCOPE|UNVISITED_STATE|Fidelity|State exploration|T1_POLITE_HTTP|frames_from_source_dom_only|collapsed_published)\b/;

describe('capture status labels', () => {
  it('has a plain label, colour and meaning for every badge', () => {
    for (const badge of BADGE_ORDER) {
      const info = BADGES[badge];
      expect(info.label).toBeTruthy();
      expect(info.meaning.length).toBeGreaterThan(20);
      expect(info.label).not.toMatch(ENGINE_TERMS);
    }
    expect(badgeInfo('NEEDS_REVIEW').label).toBe('Needs a check');
    expect(badgeInfo('VERIFIED_WITHIN_SCOPE').label).toBe('Fully captured');
  });

  it('falls back to a readable label for an unknown badge', () => {
    expect(badgeInfo('SOMETHING_NEW').label).toBe('Something new');
  });

  it('gives every issue type a title, explanation and next step', () => {
    for (const info of Object.values(ISSUES)) {
      expect(info.title).not.toMatch(ENGINE_TERMS);
      expect(info.todo).toBeTruthy();
    }
    expect(issueInfo('UNVISITED_STATE').title).toBe("A tab, menu or button wasn't opened");
  });

  it('picks the most decisive issue as the headline', () => {
    expect(headlineIssue(['UNCONFIRMED_TEXT', 'ACCESS_BLOCKED', 'UNVISITED_STATE'])).toBe('ACCESS_BLOCKED');
  });
});

describe('gap reasons become sentences', () => {
  const cases: [string, string, RegExp][] = [
    ['UNVISITED_STATE', 'unvisited_tab:policy_disallows:hover_info:Pricing', /The "Pricing" tab wasn't opened because it needs hovering, which is turned off for this run/],
    ['UNVISITED_STATE', 'unvisited_accordion:never_visible:Filter icon Cookie list', /"Filter icon Cookie list" expandable section .* never appeared on screen/],
    ['UNVISITED_STATE', 'unvisited_dialog:dialog_left_open:Life insurance', /The "Life insurance" pop-up window opened but would not close again, so its content was not kept/],
    ['UNVISITED_STATE', 'unvisited_accordion:click_failed:Sign in', /"Sign in" .* clicking it did not work\./],
    ['UNVISITED_STATE', 'unvisited_hover:click_failed:intercepted:Menu', /something on the page \(such as an overlay\) was in the way/],
    ['UNVISITED_STATE', 'unvisited_accordion:unsafe_side_effect:POST https://www.example.com/a/b:Region', /"Region" expandable section .* \(a POST request\)/],
    ['UNVISITED_STATE', 'unvisited_load_more:unsafe_side_effect:POST http://127.0.0.1:8765/reviews:Show more', /The "Show more" button wasn't opened/],
    ['UNVISITED_STATE', 'unvisited_scroll_surface:scroll_budget_exhausted:page', /stopped scrolling the page/],
    ['DISCOVERY_OPEN', 'discovery_open:unvisited_controls=3:remaining_controls=3', /3 clickable parts of the page/],
    ['DISCOVERY_OPEN', 'discovery_open:not_rendered:remaining_controls=0', /wasn't opened in a real browser/],
    ['UNSUPPORTED_FORMAT', 'ocr_engine_missing:tesseract;image_content_unextracted', /text-recognition \(OCR\) tool/],
    ['UNSUPPORTED_FORMAT', 'image_extracted;ocr_engine_missing', /text-recognition \(OCR\) tool/],
    ['DEGRADED_VARIANT', 'placeholder_content_suspected', /placeholder text/],
    ['POLICY_EXCLUDED', 'mime_not_allowed:application/x-empty', /empty response/],
    ['POLICY_EXCLUDED', 'robots_disallow:parsed', /robots\.txt/],
    ['ACCESS_BLOCKED', 'js_challenge:cloudflare:fetched_at=T1_POLITE_HTTP:host_tier=T3_FIDELITY_BROWSER', /bot protection \(Cloudflare\)/],
    ['ACCESS_BLOCKED', 'login_wall:blocks_main_content', /behind a login/],
    ['FIDELITY_DISAGREEMENT', 'frame_not_acquired:https://widgets.example.org/x', /embedded frame from widgets\.example\.org/],
    ['FIDELITY_DISAGREEMENT', 'critical_tokens_changed:lost=120¹;added=1201', /Numbers or symbols differ.*missing: 120¹; extra: 1201/],
    ['ACQUISITION_FAILED', 'rate_limited', /slow down/],
    ['LIMIT_HIT', 'truncated_body', /only the first part/],
  ];
  it.each(cases)('%s %s', (type, reason, expected) => {
    const text = describeReason(type, reason);
    expect(text.sentence).toMatch(expected);
    expect(text.sentence).not.toContain(reason);
  });

  it('falls back to the issue explanation for unknown reasons', () => {
    expect(describeReason('VOLATILE', 'some_new_reason:x').sentence).toBe(issueInfo('VOLATILE').why);
  });

  it('groups gaps with the same cause and lists their labels', () => {
    const gap = (reason: string) => ({ gap_id: reason, gap_type: 'UNVISITED_STATE', reason, retry_eligible: false, status: 'PARTIAL', tier_reached: null });
    const groups = groupGaps([gap('unvisited_tab:never_visible:A'), gap('unvisited_tab:never_visible:B'), gap('unvisited_tab:click_failed:C')]);
    expect(groups).toHaveLength(2);
    expect(groups[0]?.sentence).toBe("2 tabs weren't opened because they never appeared on screen (often a copy meant for mobile screens).");
    expect(groups[0]?.labels).toEqual(['A', 'B']);
  });
});

describe('other wording', () => {
  it('describes contradiction flags and context', () => {
    expect(describeFlag('numeric:USD:$0.0825!=$0.1580')).toBe('Different amounts: $0.0825 vs $0.1580');
    expect(describeFlag('numeric:percent:10 percent!=15 percent')).toBe('Different amounts: 10 percent vs 15 percent');
    expect(describeFlag('negation_flip')).toMatch(/"not"/);
    expect(describeContextField('codes', 'differs(PDT|HTD)').text).toBe('Different: PDT vs HTD');
    expect(describeContextField('period', 'match').text).toBe('Same');
  });

  it('describes report files', () => {
    expect(describeFile('r1_url_content.xlsx')).toMatchObject({ title: 'Page content', kind: 'Excel' });
    expect(describeFile('jh2_r1_url_content.xlsx')).toMatchObject({ title: 'Page content', kind: 'Excel' });
    expect(describeFile('jh2_r2a_overlap_groups.csv').title).toBe('Duplicate content: groups');
    expect(describeFile('jh2_r2_overlap.xlsx').title).toBe('Duplicate content (workbook)');
    expect(describeFile('jh2_manifest.json').title).toBe('Report summary');
    expect(describeFile('jh2_project_report.xlsx')).toMatchObject({ title: 'Project report', kind: 'Excel' });
    expect(describeFile('jh2_explorer.html').title).toBe('Offline page viewer');
    expect(describeFile('unknown.bin').title).toBe('unknown.bin');
  });

  it('puts blocks in the right page-area group', () => {
    expect(areaGroup({ region: 'nav_menu', role: 'nav', layer: 'rendered_visible' })).toBe('navigation');
    expect(areaGroup({ region: 'accordion_panel', role: 'main', layer: 'rendered_visible' })).toBe('main');
    expect(areaGroup({ region: null, role: 'footer', layer: 'rendered_visible' })).toBe('footer');
    expect(areaGroup({ region: null, role: 'main', layer: 'metadata' })).toBe('metadata');
  });

  it('summarises a page outcome in one sentence', () => {
    expect(outcomeSentence(row, 4)).toBe('We captured 120 words, but 3 issues mean part of the page may be missing.');
    expect(outcomeSentence({ ...row, badge: 'VERIFIED_WITHIN_SCOPE' }, 4)).toMatch(/checked all of it/);
  });
});
