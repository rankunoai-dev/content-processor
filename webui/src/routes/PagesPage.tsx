import { ClearOutlined, SettingOutlined } from '@ant-design/icons';
import {
  Button,
  Card,
  Checkbox,
  Dropdown,
  Input,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType, SorterResult } from 'antd/es/table/interface';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { PageRow, PagesResponse } from '../api/types';
import { usePages, usePrefetchPage } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { HelpTip, PageHeading, StatusTag } from '../components/bits';
import { usePreferences } from '../state/preferences';
import {
  DEFAULT_VIEW,
  PAGE_SIZES,
  pageHref,
  toPageFilters,
  useViewParams,
  type SortKey,
} from '../state/viewParams';
import {
  BADGE_ORDER,
  CHANGES,
  PAGES,
  badgeInfo,
  changeLabel,
  formatDate,
  formatLabel,
  headlineIssue,
  issueInfo,
} from '../vocabulary';

const COLUMN_KEYS = ['words', 'issues', 'type', 'modified', 'captured', 'change'] as const;

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

function SearchBox({ value, onChange }: { value: string; onChange: (q: string) => void }) {
  const [text, setText] = useState(value);
  const debounced = useDebounced(text, 350);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (debounced !== value) onChange(debounced);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);
  return (
    <Input.Search
      allowClear
      aria-label={PAGES.search}
      placeholder={PAGES.search}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onSearch={(q) => onChange(q)}
      className="search-box"
    />
  );
}

function ColumnChooser() {
  const hidden = usePreferences((s) => s.hiddenColumns);
  const toggle = usePreferences((s) => s.toggleColumn);
  const labels: Record<string, string> = {
    words: PAGES.col.words,
    issues: PAGES.col.issues,
    type: PAGES.col.type,
    modified: PAGES.col.modified,
    captured: PAGES.col.captured,
    change: PAGES.col.change,
  };
  return (
    <Dropdown
      trigger={['click']}
      menu={{
        items: COLUMN_KEYS.map((k) => ({
          key: k,
          label: (
            <Checkbox checked={!hidden.includes(k)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(k)}>
              {labels[k]}
            </Checkbox>
          ),
        })),
      }}
    >
      <Button icon={<SettingOutlined />}>{PAGES.columns}</Button>
    </Dropdown>
  );
}

export default function PagesPage() {
  const { runId = '' } = useParams();
  const [view, setView] = useViewParams();
  const filters = useMemo(() => toPageFilters(view), [view]);
  const pages = usePages(runId, filters);
  const prefetch = usePrefetchPage(runId);
  const navigate = useNavigate();
  const hidden = usePreferences((s) => s.hiddenColumns);
  const density = usePreferences((s) => s.density);
  const facets = pages.data?.facets;

  const statusOptions = useMemo(() => {
    const counts = facets?.status ?? {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    return [
      { value: '', label: `${PAGES.allStatuses} (${total})` },
      ...BADGE_ORDER.filter((b) => (counts[b] ?? 0) > 0 || view.status.includes(b)).map((b) => ({
        value: b,
        label: `${badgeInfo(b).label} (${counts[b] ?? 0})`,
      })),
    ];
  }, [facets, view.status]);

  const sortOrderFor = (key: SortKey) =>
    view.sort === key ? (view.order === 'asc' ? ('ascend' as const) : ('descend' as const)) : null;

  const columns: ColumnsType<PageRow> = [
    {
      key: 'title',
      title: PAGES.col.page,
      width: 380,
      sorter: true,
      sortOrder: sortOrderFor('title'),
      render: (_, row) => (
        <div className="page-cell">
          <a href={pageHref(runId, row.page_key)} onClick={(e) => e.preventDefault()} className="page-cell-title">
            {row.title}
          </a>
          <Space size={4} className="page-cell-meta">
            <Tag bordered={false} className="host-tag">
              {row.host}
            </Tag>
            <Typography.Text type="secondary" className="page-cell-url" ellipsis={{ tooltip: row.url }}>
              {row.url.replace(/^https?:\/\/[^/]+/, '') || '/'}
            </Typography.Text>
          </Space>
        </div>
      ),
    },
    {
      key: 'status',
      title: PAGES.col.status,
      width: 170,
      sorter: true,
      sortOrder: sortOrderFor('status'),
      render: (_, row) => <StatusTag badge={row.badge} />,
    },
    {
      key: 'words',
      title: (
        <Space size={4}>
          {PAGES.col.words}
          <HelpTip text={PAGES.colHelp.words} />
        </Space>
      ),
      width: 100,
      align: 'right',
      sorter: true,
      sortOrder: sortOrderFor('words'),
      render: (_, row) => row.words.toLocaleString(),
    },
    {
      key: 'issues',
      title: (
        <Space size={4}>
          {PAGES.col.issues}
          <HelpTip text={PAGES.colHelp.issues} />
        </Space>
      ),
      width: 250,
      sorter: true,
      sortOrder: sortOrderFor('issues'),
      render: (_, row) => {
        if (row.issues === 0) return <Typography.Text type="secondary">{PAGES.noIssues}</Typography.Text>;
        const top = headlineIssue(row.issue_keys);
        return (
          <Tooltip title={row.issue_keys.map((k) => issueInfo(k).title).join(' · ')}>
            <Space size={4} direction="vertical" className="issue-cell">
              <Typography.Text>{top ? issueInfo(top).title : ''}</Typography.Text>
              {row.issues > 1 ? (
                <Typography.Text type="secondary" className="small-note">
                  {PAGES.issuesTotal(row.issues)}
                </Typography.Text>
              ) : null}
            </Space>
          </Tooltip>
        );
      },
    },
    {
      key: 'type',
      title: PAGES.col.type,
      width: 130,
      sorter: true,
      sortOrder: sortOrderFor('type'),
      render: (_, row) => formatLabel(row.content_type),
    },
    {
      key: 'modified',
      title: (
        <Space size={4}>
          {PAGES.col.modified}
          <HelpTip text={PAGES.colHelp.modified} />
        </Space>
      ),
      width: 150,
      sorter: true,
      sortOrder: sortOrderFor('modified'),
      render: (_, row) => formatDate(row.page_modified_at) || <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      key: 'captured',
      title: PAGES.col.captured,
      width: 140,
      sorter: true,
      sortOrder: sortOrderFor('captured'),
      render: (_, row) => formatDate(row.captured_at),
    },
    {
      key: 'change',
      title: PAGES.col.change,
      width: 150,
      render: (_, row) =>
        row.change_status ? (
          <Tag bordered={false} color={CHANGES[row.change_status]?.tag}>
            {changeLabel(row.change_status)}
          </Tag>
        ) : (
          <Typography.Text type="secondary">{changeLabel(null)}</Typography.Text>
        ),
    },
  ];
  const visibleColumns = columns.filter((c) => !hidden.includes(String(c.key)));

  const filtered =
    view.status.length + view.host.length + view.format.length + view.issue.length > 0 ||
    view.hasIssues !== null ||
    view.q !== '';

  return (
    <>
      <PageHeading
        title={PAGES.title}
        subtitle={PAGES.subtitle}
        extra={pages.data ? <Typography.Text type="secondary">{PAGES.total(pages.data.total)}</Typography.Text> : null}
      />
      <Card className="toolbar-card" size="small">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div className="segmented-scroll">
            <Segmented
              aria-label={PAGES.col.status}
              options={statusOptions}
              value={view.status.length === 1 ? view.status[0] : ''}
              onChange={(v) => setView({ status: v ? [String(v)] : [] })}
            />
          </div>
          <div className="toolbar-row">
            <SearchBox value={view.q} onChange={(q) => setView({ q }, { replace: true })} />
            <Select
              aria-label={PAGES.site}
              mode="multiple"
              allowClear
              maxTagCount="responsive"
              placeholder={PAGES.sitePlaceholder}
              value={view.host}
              onChange={(host: string[]) => setView({ host })}
              options={(facets?.hosts ?? []).map((h) => ({ value: h.value, label: `${h.value} (${h.count})` }))}
              className="toolbar-select"
            />
            <Select
              aria-label={PAGES.type}
              mode="multiple"
              allowClear
              maxTagCount="responsive"
              placeholder={PAGES.typePlaceholder}
              value={view.format}
              onChange={(format: string[]) => setView({ format })}
              options={(facets?.formats ?? []).map((f) => ({ value: f.value, label: `${formatLabel(f.value)} (${f.count})` }))}
              className="toolbar-select"
            />
            <Select
              aria-label={PAGES.issue}
              mode="multiple"
              allowClear
              maxTagCount="responsive"
              placeholder={PAGES.issuePlaceholder}
              value={view.issue}
              onChange={(issue: string[]) => setView({ issue })}
              options={(facets?.issues ?? []).map((f) => ({ value: f.value, label: `${issueInfo(f.value).title} (${f.count})` }))}
              className="toolbar-select toolbar-select-wide"
              popupMatchSelectWidth={false}
            />
            <Space size={8} className="nowrap">
              <Switch
                id="only-issues"
                checked={view.hasIssues === true}
                onChange={(on) => setView({ hasIssues: on ? true : null })}
                aria-label={PAGES.onlyIssues}
              />
              <label htmlFor="only-issues">{PAGES.onlyIssues}</label>
            </Space>
            <span className="toolbar-spacer" />
            {filtered ? (
              <Button
                icon={<ClearOutlined />}
                onClick={() =>
                  setView({ status: [], host: [], format: [], issue: [], hasIssues: null, q: '' })
                }
              >
                {PAGES.clearFilters}
              </Button>
            ) : null}
            <ColumnChooser />
          </div>
        </Space>
      </Card>
      <Card className="table-card" size="small">
        <QueryState<PagesResponse>
          query={pages}
          isEmpty={(d) => d.total === 0}
          empty={filtered ? PAGES.emptyFiltered : PAGES.emptyRun}
          rows={10}
        >
          {(data) => (
            <Table<PageRow>
              rowKey="page_key"
              size={density}
              columns={visibleColumns}
              dataSource={data.items}
              loading={pages.isFetching && pages.isPlaceholderData}
              tableLayout="fixed"
              scroll={{ x: 1050 }}
              rowClassName="clickable-row"
              onRow={(row) => ({
                onClick: () => navigate(pageHref(runId, row.page_key)),
                onMouseEnter: () => prefetch(row.page_key),
                onKeyDown: (e) => {
                  if (e.key === 'Enter') navigate(pageHref(runId, row.page_key));
                },
                tabIndex: 0,
                'aria-label': `${row.title}, ${badgeInfo(row.badge).label}`,
              })}
              pagination={{
                current: data.page,
                pageSize: data.size,
                total: data.total,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZES,
                showTotal: (total, range) => PAGES.range(range[0], range[1], total),
              }}
              onChange={(pagination, _filters, sorter) => {
                const s = (Array.isArray(sorter) ? sorter[0] : sorter) as SorterResult<PageRow> | undefined;
                const sortKey = s?.order ? (String(s.columnKey) as SortKey) : DEFAULT_VIEW.sort;
                const order = s?.order === 'descend' ? 'desc' : 'asc';
                const sortChanged = sortKey !== view.sort || order !== view.order;
                setView(
                  sortChanged
                    ? { sort: sortKey, order }
                    : { page: pagination.current ?? 1, size: pagination.pageSize ?? view.size },
                );
              }}
            />
          )}
        </QueryState>
      </Card>
    </>
  );
}
