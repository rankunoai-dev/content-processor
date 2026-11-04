import { afterEach, describe, expect, it } from 'vitest';
import { useProjectTokens } from '../api/projects';

afterEach(() => {
  useProjectTokens.setState({ tokens: {} });
  window.sessionStorage.clear();
});

describe('unlocking a project', () => {
  it('is kept in this tab across a reload until it expires', async () => {
    useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z');
    useProjectTokens.getState().remember('old', 'tok-0', '2000-01-01T00:00:00Z');
    const saved = JSON.parse(window.sessionStorage.getItem('capture-results-unlocked') ?? '{}') as {
      state: { tokens: Record<string, { token: string }> };
    };
    expect(Object.keys(saved.state.tokens)).toEqual(['p1']);
    // a reload: memory is empty, the tab's storage is read back
    useProjectTokens.setState({ tokens: {} }, false);
    window.sessionStorage.setItem(
      'capture-results-unlocked',
      JSON.stringify({
        state: {
          tokens: {
            p1: { token: 'tok-1', expiresAt: Date.parse('2099-01-01T00:00:00Z') },
            old: { token: 'tok-0', expiresAt: Date.parse('2000-01-01T00:00:00Z') },
          },
        },
        version: 0,
      }),
    );
    await useProjectTokens.persist.rehydrate();
    expect(useProjectTokens.getState().tokens.p1?.token).toBe('tok-1');
    expect(useProjectTokens.getState().tokens.old).toBeUndefined();
    useProjectTokens.getState().forget('p1');
    expect(window.sessionStorage.getItem('capture-results-unlocked')).not.toContain('tok-1');
  });
});
