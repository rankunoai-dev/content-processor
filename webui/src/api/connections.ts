/**
 * Connections: the platform accounts (Semrush, Ahrefs, Search Console, GA4) saved once
 * and chosen per project. The app holds only the NAME of each key's environment
 * variable; the key itself is never sent to the browser. Saving or removing a connection
 * needs an admin; choosing one for a project needs the project unlocked.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';

export type Platform = 'semrush' | 'ahrefs' | 'gsc' | 'ga4';

export interface Connection {
  connection_id: string;
  platform: Platform;
  label: string;
  secret_env: string;
  key_set: boolean;
  property_ref: string;
  created_by: string;
  created_at: string;
  used_by: string[];
}

export interface ConnectionsResponse {
  platforms: Platform[];
  connections: Connection[];
}

export interface NewConnection {
  platform: Platform;
  label: string;
  secret_env: string;
  property_ref: string;
}

const KEY = ['connections'] as const;

async function readError(response: Response): Promise<ApiError> {
  let message = `The server answered with an error (${response.status}).`;
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === 'string') message = detail;
    }
  } catch {
    // not JSON: keep the generic sentence
  }
  return new ApiError(response.status, message);
}

async function send<T>(method: string, path: string, body?: unknown, token?: string | null): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-Requested-With': 'content-processor',
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers['X-Project-Token'] = token;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, 'Could not reach the results server. Is it still running?');
  }
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

export const connectionsApi = {
  list: () => send<ConnectionsResponse>('GET', '/api/connections'),
  add: (body: NewConnection) => send<Connection>('POST', '/api/connections', body),
  remove: (connectionId: string) =>
    send<{ removed: string }>('DELETE', `/api/connections/${encodeURIComponent(connectionId)}`),
  choose: (projectId: string, connections: Record<string, string>, token: string | null) =>
    send<{ project_id: string; connections: Record<string, string> }>(
      'PUT',
      `/api/projects/${encodeURIComponent(projectId)}/connections`,
      { connections },
      token,
    ),
};

/** Every saved connection (viewers see names and whether each key is set; never a key). */
export function useConnections() {
  return useQuery({ queryKey: KEY, queryFn: connectionsApi.list, staleTime: 30_000 });
}

export function useSaveConnection() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NewConnection) => connectionsApi.add(body),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
}

export function useRemoveConnection() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (connectionId: string) => connectionsApi.remove(connectionId),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
}

export function useChooseConnections(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { connections: Record<string, string>; token: string | null }) =>
      connectionsApi.choose(projectId, args.connections, args.token),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
}
