// Mirrors src/modules/content_processor/webapi/models.py. Keep the two in step.

export type Badge =
  | 'RUNNING'
  | 'FAILED'
  | 'BLOCKED'
  | 'PARTIAL'
  | 'NEEDS_REVIEW'
  | 'VERIFIED_WITHIN_SCOPE'
  | 'EXCLUDED';

export interface Count {
  value: string;
  count: number;
}

export interface RunSummary {
  name: string;
  run_id: string | null;
  records_file: string;
  page_count: number;
  hosts: string[];
  badges: Record<string, number>;
  fully_captured_pct: number;
  issue_count: number;
  pages_with_issues: number;
  captured_from: string | null;
  captured_to: string | null;
  file_modified_at: string;
  has_reports: boolean;
  running: number;
  unreadable_lines: number;
}

export interface RunsResponse {
  exports_dir: string;
  runs: RunSummary[];
}

export interface IssueStat {
  key: string;
  pages: number;
  occurrences: number;
  retryable_pages: number;
}

export interface RunFacts {
  mode: string | null;
  stop_reason: string | null;
  elapsed_s: number | null;
  scheduled: number | null;
}

export interface Overview {
  summary: RunSummary;
  issues: IssueStat[];
  formats: Count[];
  hosts: Count[];
  changes: Count[];
  tiers: Count[];
  words_total: number;
  facts: RunFacts | null;
}

export interface PageRow {
  page_key: string;
  url: string;
  final_url: string | null;
  title: string;
  host: string;
  badge: string;
  words: number;
  issues: number;
  issue_keys: string[];
  content_type: string | null;
  page_modified_at: string | null;
  captured_at: string;
  change_status: string | null;
  retryable: boolean;
}

export interface Facets {
  status: Record<string, number>;
  hosts: Count[];
  formats: Count[];
  issues: Count[];
}

export interface PagesResponse {
  total: number;
  page: number;
  size: number;
  items: PageRow[];
  facets: Facets;
  running: number;
}

export interface GapOut {
  gap_id: string;
  gap_type: string;
  reason: string;
  retry_eligible: boolean;
  status: string;
  tier_reached: string | null;
}

export interface Finding {
  key: string;
  unconfirmed: number;
  total: number;
}

export interface Followup {
  url: string;
  edge_kind: string | null;
  expected_mime: string | null;
  page_key: string | null;
}

export interface Block {
  id: string;
  text: string;
  heading_path: string[];
  order: number;
  layer: string;
  role: string;
  region: string | null;
  format: string | null;
  verified: boolean;
  status: string;
  confidence: number;
  page_label: string | null;
  frame: string;
}

export interface PageDetail {
  row: PageRow;
  run_name: string;
  requested_url: string;
  aliases: string[];
  page_modified_source: string | null;
  page_published_at: string | null;
  change_basis: string | null;
  previous_run_id: string | null;
  reused_from_run_id: string | null;
  tier_reached: string | null;
  sniffed_mime: string | null;
  http_last_modified: string | null;
  followups: Followup[];
  status_vector: Record<string, unknown>;
  inventory: Record<string, unknown>;
  discovery: Record<string, unknown>;
  warnings: string[];
  gaps: GapOut[];
  findings: Finding[];
  blocks: Block[];
  repeated_blocks: number;
}

export type ReportSource = 'report' | 'computed';

export interface DuplicateMember {
  url: string;
  page_key: string | null;
  title: string;
  badge: string;
  overlap_pct: number;
  shared_blocks: number;
  representative: boolean;
}

export interface DuplicateGroup {
  group_id: string;
  size: number;
  members: DuplicateMember[];
}

export interface DuplicatePair {
  url_a: string;
  url_b: string;
  key_a: string | null;
  key_b: string | null;
  title_a: string;
  title_b: string;
  jaccard_pct: number;
  a_in_b_pct: number;
  b_in_a_pct: number;
  shared_passages: string[];
  exact: boolean;
}

export interface DuplicatesResponse {
  source: ReportSource;
  generated_at: string | null;
  stale: boolean;
  metric: string;
  threshold_pct: number;
  groups: DuplicateGroup[];
  pairs: DuplicatePair[];
}

export interface Statement {
  url: string;
  page_key: string | null;
  title: string;
  quote: string;
  heading: string;
  badge: string;
}

export interface Contradiction {
  pair_id: string;
  bucket: string;
  confidence: string;
  confidence_reason: string;
  flags: string[];
  context: Record<string, string>;
  contradiction_pct: number | null;
  a: Statement;
  b: Statement;
  reviewer_decision: string;
  decided_by?: string | null;
  decided_at?: string | null;
}

export interface ContradictionsResponse {
  source: ReportSource;
  generated_at: string | null;
  stale: boolean;
  judge_status: string | null;
  items: Contradiction[];
}

export interface ReviewItem {
  url: string;
  page_key: string | null;
  title: string;
  badge: string;
  issue_keys: string[];
  gap_types: string[];
  suggested_action: string;
  operator_action: string;
  operator_note: string;
  decided_by?: string | null;
  decided_at?: string | null;
}

export interface ReviewResponse {
  source: ReportSource;
  generated_at: string | null;
  stale: boolean;
  items: ReviewItem[];
}

export interface DownloadFile {
  name: string;
  size: number;
  modified_at: string;
}

export interface DownloadsResponse {
  folder: string | null;
  files: DownloadFile[];
}

export type DecisionKind = 'contradiction' | 'review';

/** A reviewer's decision; an empty decision and note clears it. */
export interface DecisionIn {
  kind: DecisionKind;
  key: string;
  decision: string;
  note: string;
  reviewer: string;
}

export interface DecisionOut extends Omit<DecisionIn, 'reviewer'> {
  at: string;
  reviewer: string;
}


/** One content type a run can select, with what the engine supports today. */
export interface ContentType {
  id: string;
  label: string;
  families: string[];
  extensions: string[];
  status: 'supported' | 'not_supported';
  note: string;
}

export interface ContentTypesResponse {
  types: ContentType[];
}

/** What a selection means for a run: the filter, the unsupported picks and the command. */
export interface ScopeFragment {
  selected: string[];
  allowed_mime_families: string[];
  unsupported_selected: string[];
  command: string;
}
