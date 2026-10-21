import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

/**
 * Client preferences only. Server data lives in TanStack Query; shareable
 * view state (filters, sort, page, tab) lives in the URL.
 */
export interface Preferences {
  theme: 'light' | 'dark';
  density: 'middle' | 'small';
  hiddenColumns: string[];
  showTechNames: boolean;
  siderCollapsed: boolean;
  lastRun: string | null;
  /** Name recorded with every decision saved from this browser. */
  reviewer: string;
  setTheme: (theme: 'light' | 'dark') => void;
  setDensity: (density: 'middle' | 'small') => void;
  toggleColumn: (column: string) => void;
  setShowTechNames: (show: boolean) => void;
  setSiderCollapsed: (collapsed: boolean) => void;
  setLastRun: (run: string | null) => void;
  setReviewer: (reviewer: string) => void;
}

export const DEFAULT_HIDDEN_COLUMNS = ['change', 'captured'];

function safeStorage(): Storage {
  try {
    const probe = '__prefs_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    const memory = new Map<string, string>();
    return {
      get length() {
        return memory.size;
      },
      clear: () => memory.clear(),
      getItem: (k) => memory.get(k) ?? null,
      key: (i) => [...memory.keys()][i] ?? null,
      removeItem: (k) => void memory.delete(k),
      setItem: (k, v) => void memory.set(k, v),
    };
  }
}

export const usePreferences = create<Preferences>()(
  persist(
    (set) => ({
      theme: 'light',
      density: 'middle',
      hiddenColumns: DEFAULT_HIDDEN_COLUMNS,
      showTechNames: false,
      siderCollapsed: false,
      lastRun: null,
      reviewer: '',
      setTheme: (theme) => set({ theme }),
      setDensity: (density) => set({ density }),
      toggleColumn: (column) =>
        set((s) => ({
          hiddenColumns: s.hiddenColumns.includes(column)
            ? s.hiddenColumns.filter((c) => c !== column)
            : [...s.hiddenColumns, column],
        })),
      setShowTechNames: (showTechNames) => set({ showTechNames }),
      setSiderCollapsed: (siderCollapsed) => set({ siderCollapsed }),
      setLastRun: (lastRun) => set({ lastRun }),
      setReviewer: (reviewer) => set({ reviewer }),
    }),
    {
      name: 'capture-results-preferences',
      version: 1,
      storage: createJSONStorage(safeStorage),
    },
  ),
);
