import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { PageFilters } from '../api/client';
import { AREA_GROUP_ORDER, DEFAULT_AREA_GROUPS, type AreaGroup } from '../vocabulary';

/**
 * Every piece of shareable view state, kept in the URL so that back/forward
 * and copied links reproduce the view. Defaults are left out of the URL.
 */
export interface ViewParams {
  // pages table
  status: string[];
  host: string[];
  format: string[];
  issue: string[];
  hasIssues: boolean | null;
  q: string;
  sort: SortKey;
  order: 'asc' | 'desc';
  page: number;
  size: number;
  // page detail
  tab: DetailTab;
  areas: AreaGroup[];
  hidden: boolean;
  unconfirmed: boolean;
  find: string;
  // contradictions
  bucket: string;
}

export const SORT_KEYS = ['status', 'title', 'url', 'words', 'issues', 'modified', 'captured', 'type'] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export const DETAIL_TABS = ['content', 'issues', 'facts', 'technical'] as const;
export type DetailTab = (typeof DETAIL_TABS)[number];
export const PAGE_SIZES = [25, 50, 100, 200];

export const DEFAULT_VIEW: ViewParams = {
  status: [],
  host: [],
  format: [],
  issue: [],
  hasIssues: null,
  q: '',
  sort: 'status',
  order: 'asc',
  page: 1,
  size: 50,
  tab: 'content',
  areas: DEFAULT_AREA_GROUPS,
  hidden: false,
  unconfirmed: false,
  find: '',
  bucket: '',
};

const LIST_KEYS = ['status', 'host', 'format', 'issue'] as const;

function list(sp: URLSearchParams, key: string): string[] {
  return sp
    .getAll(key)
    .flatMap((v) => v.split(','))
    .map((v) => v.trim())
    .filter(Boolean);
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

function int(value: string | null, fallback: number, min: number, max: number): number {
  const n = value === null ? NaN : Number.parseInt(value, 10);
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
}

/** URL -> view state. Unknown or malformed values fall back to defaults. */
export function parseViewParams(sp: URLSearchParams): ViewParams {
  const hasIssues = sp.get('has_issues');
  const areasRaw = sp.get('areas');
  const areas =
    areasRaw === null
      ? DEFAULT_VIEW.areas
      : areasRaw
          .split(',')
          .filter((a): a is AreaGroup => (AREA_GROUP_ORDER as string[]).includes(a));
  const size = int(sp.get('size'), DEFAULT_VIEW.size, 1, 200);
  return {
    status: list(sp, 'status'),
    host: list(sp, 'host'),
    format: list(sp, 'format'),
    issue: list(sp, 'issue'),
    hasIssues: hasIssues === 'true' ? true : hasIssues === 'false' ? false : null,
    q: sp.get('q') ?? '',
    sort: oneOf(sp.get('sort'), SORT_KEYS, DEFAULT_VIEW.sort),
    order: oneOf(sp.get('order'), ['asc', 'desc'] as const, DEFAULT_VIEW.order),
    page: int(sp.get('page'), 1, 1, 100_000),
    size: PAGE_SIZES.includes(size) ? size : DEFAULT_VIEW.size,
    tab: oneOf(sp.get('tab'), DETAIL_TABS, DEFAULT_VIEW.tab),
    areas,
    hidden: sp.get('hidden') === '1',
    unconfirmed: sp.get('unconfirmed') === '1',
    find: sp.get('find') ?? '',
    bucket: sp.get('bucket') ?? '',
  };
}

/** View state -> URL. Only values that differ from the defaults are written. */
export function serializeViewParams(v: ViewParams): URLSearchParams {
  const sp = new URLSearchParams();
  for (const key of LIST_KEYS) for (const value of v[key]) sp.append(key, value);
  if (v.hasIssues !== null) sp.set('has_issues', String(v.hasIssues));
  if (v.q) sp.set('q', v.q);
  if (v.sort !== DEFAULT_VIEW.sort) sp.set('sort', v.sort);
  if (v.order !== DEFAULT_VIEW.order) sp.set('order', v.order);
  if (v.page !== 1) sp.set('page', String(v.page));
  if (v.size !== DEFAULT_VIEW.size) sp.set('size', String(v.size));
  if (v.tab !== DEFAULT_VIEW.tab) sp.set('tab', v.tab);
  const areas = AREA_GROUP_ORDER.filter((a) => v.areas.includes(a));
  if (areas.join(',') !== DEFAULT_VIEW.areas.join(',')) sp.set('areas', areas.join(','));
  if (v.hidden) sp.set('hidden', '1');
  if (v.unconfirmed) sp.set('unconfirmed', '1');
  if (v.find) sp.set('find', v.find);
  if (v.bucket) sp.set('bucket', v.bucket);
  return sp;
}

/** Filter changes send the table back to its first page. */
const RESETS_PAGE: (keyof ViewParams)[] = ['status', 'host', 'format', 'issue', 'hasIssues', 'q', 'sort', 'order', 'size'];

export function useViewParams() {
  const [searchParams, setSearchParams] = useSearchParams();
  const params = useMemo(() => parseViewParams(searchParams), [searchParams]);

  const update = useCallback(
    (patch: Partial<ViewParams>, options?: { replace?: boolean }) => {
      setSearchParams(
        (current) => {
          const next = { ...parseViewParams(current), ...patch };
          if (patch.page === undefined && RESETS_PAGE.some((k) => k in patch)) next.page = 1;
          return serializeViewParams(next);
        },
        { replace: options?.replace ?? false },
      );
    },
    [setSearchParams],
  );

  return [params, update] as const;
}

export function toPageFilters(v: ViewParams): PageFilters {
  return {
    status: v.status,
    host: v.host,
    format: v.format,
    issue: v.issue,
    hasIssues: v.hasIssues,
    q: v.q,
    sort: v.sort,
    order: v.order,
    page: v.page,
    size: v.size,
  };
}

/** Link to the pages table with some filters applied. */
export function pagesHref(run: string, patch: Partial<ViewParams>): string {
  const qs = serializeViewParams({ ...DEFAULT_VIEW, ...patch }).toString();
  return `/runs/${encodeURIComponent(run)}/pages${qs ? `?${qs}` : ''}`;
}

/** Link to one page of the run. */
export function pageHref(run: string, key: string, tab?: string): string {
  return `/runs/${encodeURIComponent(run)}/pages/${encodeURIComponent(key)}${tab ? `?tab=${tab}` : ''}`;
}
