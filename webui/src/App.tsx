import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider, theme as antTheme } from 'antd';
import { lazy, useEffect, useState, type ReactNode } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { createQueryClient } from './api/queries';
import { AppLayout } from './components/AppLayout';
import { ErrorBoundary } from './components/ErrorBoundary';
import { usePreferences } from './state/preferences';

const RunsPage = lazy(() => import('./routes/RunsPage'));
const OverviewPage = lazy(() => import('./routes/OverviewPage'));
const PagesPage = lazy(() => import('./routes/PagesPage'));
const PageDetailPage = lazy(() => import('./routes/PageDetailPage'));
const DuplicatesPage = lazy(() => import('./routes/DuplicatesPage'));
const ContradictionsPage = lazy(() => import('./routes/ContradictionsPage'));
const ReviewPage = lazy(() => import('./routes/ReviewPage'));
const DownloadsPage = lazy(() => import('./routes/DownloadsPage'));
const NotFoundPage = lazy(() => import('./routes/NotFoundPage'));
const ContentTypesPage = lazy(() => import('./routes/ContentTypesPage'));

export function ThemeProvider({ children }: { children: ReactNode }) {
  const mode = usePreferences((s) => s.theme);
  const density = usePreferences((s) => s.density);
  useEffect(() => {
    document.documentElement.dataset.theme = mode;
  }, [mode]);
  return (
    <ConfigProvider
      componentSize={density === 'small' ? 'small' : 'middle'}
      theme={{
        algorithm: mode === 'dark' ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
        token: {
          colorPrimary: '#1f5eff',
          borderRadius: 8,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
        },
        components: {
          Layout: { headerHeight: 56, headerPadding: '0 24px' },
          Card: { headerFontSize: 15 },
        },
      }}
    >
      <AntApp>{children}</AntApp>
    </ConfigProvider>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<RunsPage />} />
        <Route path="content-types" element={<ContentTypesPage />} />
        <Route path="runs/:runId" element={<OverviewPage />} />
        <Route path="runs/:runId/pages" element={<PagesPage />} />
        <Route path="runs/:runId/pages/:pageKey" element={<PageDetailPage />} />
        <Route path="runs/:runId/duplicates" element={<DuplicatesPage />} />
        <Route path="runs/:runId/contradictions" element={<ContradictionsPage />} />
        <Route path="runs/:runId/review" element={<ReviewPage />} />
        <Route path="runs/:runId/downloads" element={<DownloadsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export default function App({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? createQueryClient());
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ErrorBoundary>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ErrorBoundary>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
