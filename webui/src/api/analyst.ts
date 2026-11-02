/**
 * Senior Analyst Action Cards: API calls, types and React Query hooks.
 *
 * Reading the card library and a project's runs is open. Checking a card (preflight)
 * is a POST from this app; starting a run also needs the project's unlock token,
 * because a run writes files and, with Claude, may spend money.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';

export type CardModule = 'content' | 'theming' | 'research' | 'monitoring' | 'gap' | 'playground';
export type ParameterKind = 'project' | 'website' | 'url' | 'keyword' | 'topic' | 'text' | 'choice';
export type Backend = 'pack' | 'claude';
export type CardRunStatus = 'queued' | 'running' | 'finished' | 'failed' | 'refused';

export interface CardSummary {
  id: string;
  title: string;
  module: CardModule;
  category: string;
  icon: string;
  summary: string;
  deliverable: string;
  available: boolean;
  needs_modules: string[];
}

export interface CardParameter {
  name: string;
  label: string;
  kind: ParameterKind;
  description: string;
  required: boolean;
  default: string | null;
  choices: string[];
  many: boolean;
}

export interface ActionCard {
  id: string;
  version: number;
  title: string;
  module: CardModule;
  category: string;
  summary: string;
  deliverable: string;
  parameters: CardParameter[];
  required_tools: string[];
  optional_tools: string[];
  output_format: string;
}

export type CardInputs = Record<string, string | string[] | null>;

export interface Preflight {
  card_id: string;
  runnable: boolean;
  inputs: Record<string, unknown>;
  problems: string[];
  warnings: string[];
  tools: string[];
  missing_tools: string[];
}

export interface Verification {
  references: number;
  resolved: number;
  unknown_refs: string[];
  quotes_checked: number;
  quotes_failed: string[];
  uncited_lines: string[];
  passed: boolean;
}

export interface CardRun {
  run_id: string;
  card_id: string;
  card_version: number;
  backend: Backend;
  status: CardRunStatus;
  created_at: string;
  finished_at: string | null;
  inputs: Record<string, unknown>;
  project_ids: string[];
  model: string | null;
  tool_calls: number;
  turns: number;
  usage: Record<string, number>;
  cost_usd: number;
  cap_usd: number | null;
  message: string;
  verification: Verification | null;
  files: Record<string, string>;
}

export interface RunEvent {
  at: string;
  type: string;
  [key: string]: unknown;
}

export interface CardRunDetail {
  record: CardRun;
  text: string | null;
  events: RunEvent[];
}

export const CARD_ACTIVE: ReadonlySet<CardRunStatus> = new Set(['queued', 'running']);

// -- calls ------------------------------------------------------------------------------

const enc = encodeURIComponent;

async function message(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === 'string') return d;
    }
  } catch {
    // not JSON
  }
  return fallback;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await message(response, `The server answered with an error (${response.status}).`));
  }
  return (await response.json()) as T;
}

async function post<T>(path: string, body: unknown, token?: string | null): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'X-Requested-With': 'content-processor',
  };
  if (token) headers['X-Project-Token'] = token;
  let response: Response;
  try {
    response = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await message(response, `The card could not be started (${response.status}).`));
  }
  return (await response.json()) as T;
}

const base = (projectId: string) => `/api/projects/${enc(projectId)}/analyst`;

export const analystApi = {
  cards: (signal?: AbortSignal) => get<CardSummary[]>('/api/analyst/cards', signal),
  card: (id: string, signal?: AbortSignal) => get<ActionCard>(`/api/analyst/cards/${enc(id)}`, signal),
  preflight: (projectId: string, card: string, inputs: CardInputs, backend: Backend) =>
    post<Preflight>(`${base(projectId)}/preflight`, { card, inputs, backend }),
  start: (projectId: string, card: string, inputs: CardInputs, backend: Backend, token: string | null) =>
    post<CardRun>(`${base(projectId)}/runs`, { card, inputs, backend }, token),
  runs: (projectId: string, signal?: AbortSignal) => get<CardRun[]>(`${base(projectId)}/runs`, signal),
  run: (projectId: string, runId: string, signal?: AbortSignal) =>
    get<CardRunDetail>(`${base(projectId)}/runs/${enc(runId)}`, signal),
};

// -- hooks -------------------------------------------------------------------------------

export const analystKeys = {
  cards: () => ['analyst', 'cards'] as const,
  card: (id: string) => ['analyst', 'cards', id] as const,
  runs: (projectId: string) => ['analyst', 'runs', projectId] as const,
  run: (projectId: string, runId: string) => ['analyst', 'runs', projectId, runId] as const,
};

const POLL_MS = 2000;

export function useCards() {
  return useQuery({ queryKey: analystKeys.cards(), queryFn: ({ signal }) => analystApi.cards(signal) });
}

export function useCard(id: string | null) {
  return useQuery({
    queryKey: analystKeys.card(id ?? ''),
    queryFn: ({ signal }) => analystApi.card(id ?? '', signal),
    enabled: id !== null,
  });
}

/** The project's card runs; polled while one is queued or running. */
export function useCardRuns(projectId: string) {
  return useQuery({
    queryKey: analystKeys.runs(projectId),
    queryFn: ({ signal }) => analystApi.runs(projectId, signal),
    refetchInterval: (q) => (q.state.data?.some((r) => CARD_ACTIVE.has(r.status)) ? POLL_MS : false),
  });
}

/** One run with its deliverable and events; polled until it ends. */
export function useCardRun(projectId: string, runId: string | null) {
  return useQuery({
    queryKey: analystKeys.run(projectId, runId ?? ''),
    queryFn: ({ signal }) => analystApi.run(projectId, runId ?? '', signal),
    enabled: runId !== null,
    refetchInterval: (q) => (q.state.data && CARD_ACTIVE.has(q.state.data.record.status) ? POLL_MS : false),
  });
}

export function usePreflight(projectId: string) {
  return useMutation({
    mutationFn: (args: { card: string; inputs: CardInputs; backend: Backend }) =>
      analystApi.preflight(projectId, args.card, args.inputs, args.backend),
  });
}

export function useStartCard(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { card: string; inputs: CardInputs; backend: Backend; token: string | null }) =>
      analystApi.start(projectId, args.card, args.inputs, args.backend, args.token),
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: analystKeys.runs(projectId) });
    },
  });
}
