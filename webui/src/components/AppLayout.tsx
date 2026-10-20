import {
  AppstoreOutlined,
  BarChartOutlined,
  CopyOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  FileSearchOutlined,
  MoonOutlined,
  SettingOutlined,
  SunOutlined,
  SwapOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import { Button, Divider, Drawer, Layout, Menu, Radio, Select, Space, Switch, Tooltip, Typography } from 'antd';
import { Suspense, useEffect, useMemo, useState } from 'react';
import { Link, Outlet, useLocation, useMatch, useNavigate } from 'react-router-dom';
import { useOverview, useRuns } from '../api/queries';
import { usePreferences } from '../state/preferences';
import { APP, RUNS } from '../vocabulary';
import { ErrorBoundary } from './ErrorBoundary';
import { RouteFallback } from './RouteFallback';

const { Header, Sider, Content } = Layout;

function RunPicker({ current }: { current: string | undefined }) {
  const runs = useRuns();
  const navigate = useNavigate();
  const location = useLocation();
  const options = (runs.data?.runs ?? []).map((r) => ({
    value: r.name,
    label: `${r.name} · ${RUNS.pages(r.page_count)}`,
  }));
  return (
    <Select
      aria-label={APP.runPicker}
      className="run-picker"
      placeholder={APP.runPickerPlaceholder}
      value={current}
      loading={runs.isPending}
      options={options}
      showSearch
      optionFilterProp="label"
      onChange={(value: string) => {
        // Keep the same screen when switching runs, but drop page-specific parts.
        const section = /^\/runs\/[^/]+\/(pages|duplicates|contradictions|review|downloads)/.exec(
          location.pathname,
        )?.[1];
        navigate(`/runs/${encodeURIComponent(value)}${section ? `/${section}` : ''}`);
      }}
      popupMatchSelectWidth={false}
    />
  );
}

function SettingsDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const prefs = usePreferences();
  return (
    <Drawer title={APP.settings} open={open} onClose={onClose} width={360}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Text strong>{APP.theme}</Typography.Text>
          <div className="setting-control">
            <Radio.Group
              value={prefs.theme}
              onChange={(e) => prefs.setTheme(e.target.value as 'light' | 'dark')}
              optionType="button"
              options={[
                { value: 'light', label: APP.themeLight },
                { value: 'dark', label: APP.themeDark },
              ]}
            />
          </div>
        </div>
        <div>
          <Typography.Text strong>{APP.density}</Typography.Text>
          <div className="setting-control">
            <Radio.Group
              value={prefs.density}
              onChange={(e) => prefs.setDensity(e.target.value as 'middle' | 'small')}
              optionType="button"
              options={[
                { value: 'middle', label: APP.densityComfortable },
                { value: 'small', label: APP.densityCompact },
              ]}
            />
          </div>
        </div>
        <Divider style={{ margin: 0 }} />
        <div>
          <Space align="start">
            <Switch
              id="tech-names"
              checked={prefs.showTechNames}
              onChange={prefs.setShowTechNames}
              aria-label={APP.techNames}
            />
            <label htmlFor="tech-names">
              <Typography.Text strong>{APP.techNames}</Typography.Text>
              <br />
              <Typography.Text type="secondary">{APP.techNamesHelp}</Typography.Text>
            </label>
          </Space>
        </div>
      </Space>
    </Drawer>
  );
}

/** Polls the overview of the open run, so every run screen knows whether it is still running. */
function RunWatcher({ run }: { run: string }) {
  useOverview(run);
  return null;
}

export function AppLayout() {
  const location = useLocation();
  const runMatch = useMatch('/runs/:runId/*');
  const runId = runMatch?.params.runId;
  const prefs = usePreferences();
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    if (runId && runId !== prefs.lastRun) prefs.setLastRun(runId);
  }, [runId, prefs]);

  const selected = useMemo(() => {
    if (!runId) return 'runs';
    const section = location.pathname.split('/')[3];
    return section ?? 'overview';
  }, [location.pathname, runId]);

  const base = runId ? `/runs/${encodeURIComponent(runId)}` : '';
  const items = [
    { key: 'runs', icon: <AppstoreOutlined />, label: <Link to="/">{APP.nav.runs}</Link> },
    ...(runId
      ? [
          { type: 'divider' as const },
          { key: 'overview', icon: <BarChartOutlined />, label: <Link to={base}>{APP.nav.overview}</Link> },
          { key: 'pages', icon: <UnorderedListOutlined />, label: <Link to={`${base}/pages`}>{APP.nav.pages}</Link> },
          { key: 'duplicates', icon: <CopyOutlined />, label: <Link to={`${base}/duplicates`}>{APP.nav.duplicates}</Link> },
          { key: 'contradictions', icon: <SwapOutlined />, label: <Link to={`${base}/contradictions`}>{APP.nav.contradictions}</Link> },
          { key: 'review', icon: <ExclamationCircleOutlined />, label: <Link to={`${base}/review`}>{APP.nav.review}</Link> },
          { key: 'downloads', icon: <DownloadOutlined />, label: <Link to={`${base}/downloads`}>{APP.nav.downloads}</Link> },
        ]
      : []),
  ];

  const dark = prefs.theme === 'dark';
  return (
    <Layout className="app-layout">
      <Sider
        collapsible
        collapsed={prefs.siderCollapsed}
        onCollapse={prefs.setSiderCollapsed}
        width={232}
        theme="light"
        className="app-sider"
        breakpoint="lg"
      >
        <Link to="/" className="brand" aria-label={APP.name}>
          <FileSearchOutlined className="brand-icon" />
          {!prefs.siderCollapsed && <span className="brand-name">{APP.shortName}</span>}
        </Link>
        <Menu mode="inline" selectedKeys={[selected]} items={items} theme="light" />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space size="middle" className="app-header-left">
            {runId ? (
              <>
                <Typography.Text type="secondary">{APP.runPicker}</Typography.Text>
                <RunPicker current={runId} />
              </>
            ) : (
              <Typography.Text strong>{APP.name}</Typography.Text>
            )}
          </Space>
          <Space>
            <Tooltip title={dark ? APP.toLight : APP.toDark}>
              <Button
                type="text"
                icon={dark ? <SunOutlined /> : <MoonOutlined />}
                aria-label={dark ? APP.toLight : APP.toDark}
                onClick={() => prefs.setTheme(dark ? 'light' : 'dark')}
              />
            </Tooltip>
            <Tooltip title={APP.settings}>
              <Button
                type="text"
                icon={<SettingOutlined />}
                aria-label={APP.settings}
                onClick={() => setSettingsOpen(true)}
              />
            </Tooltip>
          </Space>
        </Header>
        <Content className="app-content">
          {runId ? <RunWatcher run={runId} /> : null}
          <ErrorBoundary key={location.pathname}>
            <Suspense fallback={<RouteFallback />}>
              <Outlet />
            </Suspense>
          </ErrorBoundary>
        </Content>
      </Layout>
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </Layout>
  );
}
