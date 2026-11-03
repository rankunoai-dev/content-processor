/**
 * The modules of the platform, in the order of the home screen. A module is a view of
 * the shared projects: opening a project from a module lands on that module's tab.
 */
import {
  AimOutlined,
  ApartmentOutlined,
  BulbOutlined,
  CloudDownloadOutlined,
  LineChartOutlined,
  RadarChartOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { ComponentType } from 'react';
import type { ProjectStatus } from './api/workspace';
import { MODULES_TEXT, formatDate } from './vocabulary';

export interface ModuleDef {
  key: string;
  /** The project tab this module opens. */
  tab: string;
  /** The module's icon component (render as <m.Icon />). */
  Icon: ComponentType;
  title: string;
  description: string;
  /** This project's state in the module, in a few words. */
  status: (p: ProjectStatus) => string;
  /** Whether the module has something for this project yet. */
  ready: (p: ProjectStatus) => boolean;
}

const T = MODULES_TEXT.modules;
const date = (iso: string | null) => (iso ? formatDate(iso) : '');

export const MODULES: ModuleDef[] = [
  {
    key: 'scraper',
    tab: 'sites',
    Icon: CloudDownloadOutlined,
    ...T.scraper,
    ready: (p) => p.finished_scrapes > 0,
    status: (p) =>
      p.finished_scrapes > 0 ? T.scraper.done(p.finished_scrapes, date(p.last_scrape)) : T.scraper.none,
  },
  {
    key: 'search',
    tab: 'search',
    Icon: SearchOutlined,
    ...T.search,
    ready: (p) => p.searchable_scrapes > 0,
    status: (p) => (p.searchable_scrapes > 0 ? T.search.done(p.searchable_scrapes) : T.search.none),
  },
  {
    key: 'themes',
    tab: 'themes',
    Icon: ApartmentOutlined,
    ...T.themes,
    ready: (p) => p.themes_built !== null,
    status: (p) => (p.themes_built ? T.themes.done(p.themes, date(p.themes_built)) : T.themes.none),
  },
  {
    key: 'monitoring',
    tab: 'monitoring',
    Icon: RadarChartOutlined,
    ...T.monitoring,
    ready: (p) => p.last_monitoring_check !== null,
    status: (p) =>
      p.last_monitoring_check
        ? T.monitoring.done(p.alerts, p.high_alerts, date(p.last_monitoring_check))
        : T.monitoring.none,
  },
  {
    key: 'research',
    tab: 'research',
    Icon: LineChartOutlined,
    ...T.research,
    ready: (p) => p.research_added !== null,
    status: (p) => (p.research_added ? T.research.done(p.keywords, date(p.research_added)) : T.research.none),
  },
  {
    key: 'gaps',
    tab: 'gaps',
    Icon: AimOutlined,
    ...T.gaps,
    ready: (p) => p.gaps_built !== null,
    status: (p) =>
      p.gaps_built
        ? T.gaps.done(p.findings, p.open_findings, p.high_findings, date(p.gaps_built))
        : T.gaps.none,
  },
  {
    key: 'analyst',
    tab: 'analyst',
    Icon: BulbOutlined,
    ...T.analyst,
    ready: (p) => p.finished_scrapes > 0,
    status: (p) => (p.finished_scrapes > 0 ? T.analyst.done : T.analyst.none),
  },
];

export function moduleByKey(key: string | undefined): ModuleDef | undefined {
  return MODULES.find((m) => m.key === key);
}

export function moduleByTab(tab: string | null | undefined): ModuleDef | undefined {
  return MODULES.find((m) => m.tab === tab);
}
