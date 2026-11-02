/**
 * The project's "Themes" tab: build the theme map, see each theme's ownership and
 * pages per website, its topics and pillar pages, and the funnel per website.
 */
import { BranchesOutlined, DownloadOutlined } from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Empty,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useMemo, useState } from 'react';
import type { Project } from '../api/projects';
import { themesApi, useBuildThemes, useThemeMap, type Theme, type ThemeMap } from '../api/themes';
import { QueryState } from '../components/QueryState';
import { THEMES } from '../vocabulary';

const STAGES = ['learn', 'compare', 'decide', 'onboard', 'support', 'unknown'];
const OWNERSHIP_ORDER = ['client_gap', 'competitor_led', 'contested', 'client_owned', 'client_only'];

function OwnershipTag({ value }: { value: string }) {
  return <Tag color={THEMES.ownershipColor[value]}>{THEMES.ownership[value] ?? value}</Tag>;
}

function ThemeDetail({ theme, map }: { theme: Theme; map: ThemeMap }) {
  const names = Object.fromEntries(map.websites.map((w) => [w.website, w.name]));
  return (
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label={THEMES.why}>{theme.ownership_reason}</Descriptions.Item>
      <Descriptions.Item label={THEMES.terms}>{theme.terms.join(', ')}</Descriptions.Item>
      <Descriptions.Item label={THEMES.pillars}>
        <Space direction="vertical" size={2}>
          {theme.coverage.map((c) => (
            <Typography.Text key={c.website}>
              <Typography.Text strong>{names[c.website] ?? c.website}: </Typography.Text>
              {c.pillar ? (
                <Tooltip title={c.pillar.reasons.join(' · ')}>
                  <a href={c.pillar.url} target="_blank" rel="noreferrer noopener">
                    {c.pillar.title ?? c.pillar.url}
                  </a>
                </Tooltip>
              ) : (
                <Typography.Text type="secondary">
                  {c.pages > 0 ? THEMES.noPillar : THEMES.pageCount(0)}
                </Typography.Text>
              )}
            </Typography.Text>
          ))}
        </Space>
      </Descriptions.Item>
      {theme.topics.length > 0 ? (
        <Descriptions.Item label={THEMES.topics}>
          <Space direction="vertical" size={2}>
            {theme.topics.map((t) => (
              <Typography.Text key={t.topic_id}>
                {t.label}{' '}
                <Typography.Text type="secondary">
                  ({Object.entries(t.websites)
                    .map(([site, n]) => `${names[site] ?? site} ${n}`)
                    .join(', ')})
                </Typography.Text>
              </Typography.Text>
            ))}
          </Space>
        </Descriptions.Item>
      ) : null}
      <Descriptions.Item label={THEMES.funnel}>
        {STAGES.filter((s) => theme.funnel[s])
          .map((s) => `${THEMES.stages[s]} ${theme.funnel[s]}`)
          .join(' · ')}
      </Descriptions.Item>
      {theme.offerings.length > 0 ? (
        <Descriptions.Item label={THEMES.offerings}>{theme.offerings.join(', ')}</Descriptions.Item>
      ) : null}
    </Descriptions>
  );
}

function FunnelTable({ map }: { map: ThemeMap }) {
  const rows = map.websites.map((w) => {
    const counts: Record<string, number> = {};
    for (const p of map.pages) if (p.website === w.website) counts[p.funnel_stage] = (counts[p.funnel_stage] ?? 0) + 1;
    return { key: w.website, name: `${w.name} (${w.role})`, ...counts };
  });
  return (
    <Table
      size="small"
      pagination={false}
      dataSource={rows}
      columns={[
        { title: THEMES.websites, dataIndex: 'name' },
        ...STAGES.map((s) => ({ title: THEMES.stages[s], dataIndex: s, render: (v: number | undefined) => v ?? 0 })),
      ]}
    />
  );
}

function MapView({ map }: { map: ThemeMap }) {
  const [filter, setFilter] = useState<string>('all');
  const shown = useMemo(
    () => (filter === 'all' ? map.themes : map.themes.filter((t) => t.ownership === filter)),
    [map.themes, filter],
  );
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const t of map.themes) out[t.ownership] = (out[t.ownership] ?? 0) + 1;
    return out;
  }, [map.themes]);
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {map.skipped.length > 0 ? (
        <Alert type="warning" showIcon message={`${THEMES.skipped} ${map.skipped.join('; ')}`} />
      ) : null}
      <Segmented
        aria-label={THEMES.filter}
        value={filter}
        onChange={(v) => setFilter(String(v))}
        options={[
          { value: 'all', label: `${THEMES.all} (${map.themes.length})` },
          ...OWNERSHIP_ORDER.filter((o) => counts[o]).map((o) => ({
            value: o,
            label: `${THEMES.ownership[o]} (${counts[o]})`,
          })),
        ]}
      />
      <Table<Theme>
        rowKey="theme_id"
        size="small"
        dataSource={shown}
        pagination={{ pageSize: 25, hideOnSinglePage: true }}
        expandable={{ expandedRowRender: (t) => <ThemeDetail theme={t} map={map} /> }}
        columns={[
          { title: THEMES.columns.theme, dataIndex: 'label' },
          {
            title: THEMES.columns.owner,
            dataIndex: 'ownership',
            render: (o: string) => <OwnershipTag value={o} />,
          },
          ...map.websites.map((w) => ({
            title: w.role === 'client' ? <strong>{w.name}</strong> : w.name,
            key: w.website,
            render: (_v: unknown, t: Theme) => t.coverage.find((c) => c.website === w.website)?.pages ?? 0,
          })),
        ]}
      />
      <Card size="small" title={THEMES.funnelTitle}>
        <FunnelTable map={map} />
      </Card>
    </Space>
  );
}

export function ThemesTab({ project, token }: { project: Project; token: string | null }) {
  const map = useThemeMap(project.project_id);
  const build = useBuildThemes(project.project_id);
  const { message } = AntApp.useApp();
  const run = () => build.mutate(token, { onError: (e) => void message.error(e.message) });
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message={THEMES.intro} />
      <QueryState query={map} rows={6}>
        {(m) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Space wrap>
              <Tooltip title={token === null ? THEMES.needsUnlock : undefined}>
                <Button
                  type={m === null ? 'primary' : 'default'}
                  icon={<BranchesOutlined />}
                  disabled={token === null}
                  loading={build.isPending}
                  onClick={run}
                >
                  {build.isPending ? THEMES.building : m === null ? THEMES.build : THEMES.rebuild}
                </Button>
              </Tooltip>
              {m !== null ? (
                <Button icon={<DownloadOutlined />} href={themesApi.workbookUrl(project.project_id)}>
                  {THEMES.workbook}
                </Button>
              ) : null}
              {m !== null ? (
                <Typography.Text type="secondary">
                  {THEMES.built(new Date(m.built_at).toLocaleString())} · {THEMES.stats(m.stats)}
                </Typography.Text>
              ) : null}
            </Space>
            {token === null ? <Typography.Text type="secondary">{THEMES.needsUnlock}</Typography.Text> : null}
            {m === null ? <Empty description={THEMES.empty} /> : <MapView map={m} />}
          </Space>
        )}
      </QueryState>
    </Space>
  );
}
