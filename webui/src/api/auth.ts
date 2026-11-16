/**
 * Team accounts: who is signed in, signing in and out, one's own password, and (admins)
 * the accounts themselves. The session is an HttpOnly cookie the browser sends by itself.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';
import { moduleRequest } from './research';

export type Role = 'viewer' | 'reviewer' | 'editor' | 'admin';
export const ROLE_ORDER: Role[] = ['viewer', 'reviewer', 'editor', 'admin'];

export interface Account {
  username: string;
  display_name: string;
  role: Role;
  must_change_password: boolean;
  disabled: boolean;
  created_at: string;
  created_by: string;
  last_login_at: string | null;
}

export interface Me {
  accounts: boolean;
  account: Account | null;
  min_password: number;
}

export interface NewAccount {
  username: string;
  display_name: string;
  role: Role;
  password: string;
}

export function roleAtLeast(role: Role | undefined, wanted: Role): boolean {
  return role !== undefined && ROLE_ORDER.indexOf(role) >= ROLE_ORDER.indexOf(wanted);
}

const post = <T,>(url: string, body: unknown) =>
  moduleRequest<T>(url, { method: 'POST', body: JSON.stringify(body) });

export const authApi = {
  me: (signal?: AbortSignal) => moduleRequest<Me>('/api/auth/me', { signal }),
  login: (username: string, password: string) => post<Me>('/api/auth/login', { username, password }),
  logout: () => post<{ signed_out: boolean }>('/api/auth/logout', {}),
  changePassword: (current: string, next: string) => post<Me>('/api/auth/password', { current, new: next }),
  accounts: (signal?: AbortSignal) => moduleRequest<Account[]>('/api/accounts', { signal }),
  addAccount: (body: NewAccount) => post<Account>('/api/accounts', body),
  updateAccount: (username: string, body: Partial<Pick<Account, 'display_name' | 'role' | 'disabled'>>) =>
    post<Account>(`/api/accounts/${encodeURIComponent(username)}/update`, body),
  resetPassword: (username: string, password: string) =>
    post<Account>(`/api/accounts/${encodeURIComponent(username)}/password`, { password }),
};

export const authKeys = {
  me: ['auth', 'me'] as const,
  accounts: ['auth', 'accounts'] as const,
};

/** Who is signed in. `data.accounts` false: no accounts yet, the app is open (this machine only). */
const NO_ACCOUNTS: Me = { accounts: false, account: null, min_password: 10 };

export function useMe() {
  return useQuery({
    queryKey: authKeys.me,
    queryFn: async ({ signal }) => {
      try {
        return await authApi.me(signal);
      } catch (error) {
        // a server without this route (an older one) has no accounts: never an error state,
        // which would refetch on every mount and keep the gate on its spinner
        if (error instanceof ApiError && error.isClientError) return NO_ACCOUNTS;
        throw error;
      }
    },
    staleTime: 60_000,
    retry: 1,
    retryOnMount: false,
  });
}

/** True when the signed-in person may do this (always true while there are no accounts). */
export function useCan(wanted: Role): boolean {
  const me = useMe();
  if (!me.data?.accounts) return true;
  return roleAtLeast(me.data.account?.role, wanted);
}

export function useAccounts(enabled: boolean) {
  return useQuery({ queryKey: authKeys.accounts, queryFn: ({ signal }) => authApi.accounts(signal), enabled });
}

/** After signing in, out or changing a password: everything shown may change. */
export function useAfterAuthChange() {
  const client = useQueryClient();
  return (me?: Me) => {
    if (me) client.setQueryData(authKeys.me, me);
    void client.invalidateQueries();
  };
}

export function useAccountChange<A>(fn: (args: A) => Promise<Account>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => void client.invalidateQueries({ queryKey: authKeys.accounts }),
  });
}
