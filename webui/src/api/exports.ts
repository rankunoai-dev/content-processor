/**
 * Downloads: every report of a project (one per module) in Excel, CSV or Word, built
 * on request from each module's latest data, and everything at once as a zip.
 */
import { useQuery } from '@tanstack/react-query';
import { moduleRequest } from './research';

export type ExportFormat = 'xlsx' | 'csv' | 'docx';

export interface ExportReport {
  key: string;
  module: string;
  title: string;
  description: string;
  available: boolean;
  missing: string;
  subtitle: string;
  rows: number;
  formats: ExportFormat[];
}

export interface ExportCatalog {
  formats: { key: ExportFormat; label: string }[];
  reports: ExportReport[];
}

const enc = encodeURIComponent;

export const exportsApi = {
  catalog: (projectId: string, signal?: AbortSignal) =>
    moduleRequest<ExportCatalog>(`/api/projects/${enc(projectId)}/exports`, { signal }),
  url: (projectId: string, key: string, format: ExportFormat) =>
    `/api/projects/${enc(projectId)}/exports/${enc(key)}.${format}`,
  allUrl: (projectId: string) => `/api/projects/${enc(projectId)}/exports/all.zip`,
  cardResultUrl: (projectId: string, runId: string, format: 'md' | 'docx') =>
    `/api/projects/${enc(projectId)}/analyst/runs/${enc(runId)}/result/result.${format}`,
};

export function useExportCatalog(projectId: string) {
  return useQuery({
    queryKey: ['exports', projectId] as const,
    queryFn: ({ signal }) => exportsApi.catalog(projectId, signal),
  });
}
