import { App } from 'antd';
import { useCallback } from 'react';
import { useSaveDecision } from '../api/queries';
import type { DecisionKind } from '../api/types';
import { SAVING } from '../vocabulary';
import { usePreferences } from './preferences';

/** Save one decision with a confirmation message; refuses politely without a name. */
export function useDecisionSaver(run: string) {
  const { message } = App.useApp();
  const reviewer = usePreferences((s) => s.reviewer).trim();
  const mutation = useSaveDecision(run);
  const save = useCallback(
    async (kind: DecisionKind, key: string, decision: string, note = '') => {
      if (!reviewer) {
        void message.warning(SAVING.needName);
        return false;
      }
      try {
        await mutation.mutateAsync({ kind, key, decision, note, reviewer });
        void message.success(decision || note ? SAVING.saved : SAVING.cleared);
        return true;
      } catch (error) {
        void message.error(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    [message, mutation, reviewer],
  );
  return { save, saving: mutation.isPending, canSave: reviewer.length > 0 };
}
