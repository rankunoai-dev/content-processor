import { CrownOutlined } from '@ant-design/icons';
import { Button, Card, Col, Drawer, Progress, Row, Segmented, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import type { DuplicateGroup, DuplicatePair, DuplicatesResponse } from '../api/types';
import { useDuplicates } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { HelpTip, PageHeading, PageLink, ReportSourceNote, StatusTag } from '../components/bits';
import { usePreferences } from '../state/preferences';
import { DUPLICATES, PAGES } from '../vocabulary';

function GroupCard({ run, group }: { run: string; group: DuplicateGroup }) {
  const density = usePreferences((s) => s.density);
  const rep = group.members.find((m) => m.representative);
  return (
    <Card
      className="dup-group"
      title={DUPLICATES.groupTitle(group.size)}
      extra={<Tag bordered={false}>{group.group_id}</Tag>}
    >
      {rep ? (
        <div className="dup-rep">
          <Tooltip title={DUPLICATES.representativeHelp}>
            <Tag color="gold" icon={<CrownOutlined />} bordered={false}>
              {DUPLICATES.representative}
            </Tag>
          </Tooltip>
          <PageLink run={run} pageKey={rep.page_key} title={rep.title} url={rep.url} />
        </div>
      ) : null}
      <Table
        size={density}
        rowKey="url"
        dataSource={group.members}
        pagination={group.members.length > 10 ? { pageSize: 10, size: 'small' } : false}
        tableLayout="fixed"
        columns={[
          {
            key: 'page',
            title: PAGES.col.page,
            render: (_, m) => <PageLink run={run} pageKey={m.page_key} title={m.title} url={m.url} />,
          },
          { key: 'status', title: PAGES.col.status, width: 170, render: (_, m) => <StatusTag badge={m.badge} /> },
          {
            key: 'overlap',
            title: (
              <Space size={4}>
                {DUPLICATES.overlap}
                <HelpTip text={DUPLICATES.overlapHelp} />
              </Space>
            ),
            width: 220,
            render: (_, m) => <Progress percent={m.overlap_pct} size="small" format={(p) => `${Math.round(p ?? 0)}%`} />,
          },
          { key: 'shared', title: DUPLICATES.sharedBlocks, width: 140, align: 'right', render: (_, m) => m.shared_blocks },
        ]}
      />
    </Card>
  );
}

function PairDrawer({ run, pair, onClose }: { run: string; pair: DuplicatePair | null; onClose: () => void }) {
  return (
    <Drawer open={pair !== null} onClose={onClose} width={960} title={DUPLICATES.pairTitle}>
      {pair ? (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Row gutter={16}>
            {([
              ['A', pair.title_a, pair.url_a, pair.key_a, pair.a_in_b_pct, DUPLICATES.aInB],
              ['B', pair.title_b, pair.url_b, pair.key_b, pair.b_in_a_pct, DUPLICATES.bInA],
            ] as const).map(([side, title, url, key, pct, label]) => (
              <Col span={12} key={side}>
                <Card size="small" title={side === 'A' ? DUPLICATES.pageA : DUPLICATES.pageB}>
                  <PageLink run={run} pageKey={key} title={title} url={url} />
                  <div className="pair-stat">
                    <Typography.Text type="secondary">{label}</Typography.Text>
                    <Progress percent={pct} size="small" format={(p) => `${Math.round(p ?? 0)}%`} />
                  </div>
                </Card>
              </Col>
            ))}
          </Row>
          <Space>
            <Typography.Text strong>{DUPLICATES.similarity}:</Typography.Text>
            <Typography.Text>{Math.round(pair.jaccard_pct)}%</Typography.Text>
            {pair.exact ? <Tag color="red" bordered={false}>{DUPLICATES.exact}</Tag> : null}
          </Space>
          <div>
            <Typography.Title level={5}>{DUPLICATES.sharedText}</Typography.Title>
            {pair.shared_passages.map((p, i) => (
              <blockquote key={i} className="quote">
                {p}
              </blockquote>
            ))}
          </div>
        </Space>
      ) : null}
    </Drawer>
  );
}

function PairsTable({ run, pairs }: { run: string; pairs: DuplicatePair[] }) {
  const [open, setOpen] = useState<DuplicatePair | null>(null);
  const density = usePreferences((s) => s.density);
  return (
    <Card size="small" className="table-card">
      <Table<DuplicatePair>
        size={density}
        rowKey={(p) => `${p.url_a}|${p.url_b}`}
        dataSource={pairs}
        pagination={{ pageSize: 25, showSizeChanger: false }}
        tableLayout="fixed"
        columns={[
          { key: 'a', title: DUPLICATES.pageA, render: (_, p) => <PageLink run={run} pageKey={p.key_a} title={p.title_a} url={p.url_a} /> },
          { key: 'b', title: DUPLICATES.pageB, render: (_, p) => <PageLink run={run} pageKey={p.key_b} title={p.title_b} url={p.url_b} /> },
          {
            key: 'sim',
            title: (
              <Space size={4}>
                {DUPLICATES.similarity}
                <HelpTip text={DUPLICATES.similarityHelp} />
              </Space>
            ),
            width: 200,
            sorter: (x, y) => x.jaccard_pct - y.jaccard_pct,
            defaultSortOrder: 'descend',
            render: (_, p) => <Progress percent={p.jaccard_pct} size="small" format={(v) => `${Math.round(v ?? 0)}%`} />,
          },
          {
            key: 'go',
            title: '',
            width: 110,
            render: (_, p) => (
              <Button size="small" onClick={() => setOpen(p)}>
                {DUPLICATES.viewPair}
              </Button>
            ),
          },
        ]}
      />
      <PairDrawer run={run} pair={open} onClose={() => setOpen(null)} />
    </Card>
  );
}

export default function DuplicatesPage() {
  const { runId = '' } = useParams();
  const dups = useDuplicates(runId);
  const [view, setView] = useState<'groups' | 'pairs'>('groups');
  return (
    <>
      <PageHeading title={DUPLICATES.title} subtitle={DUPLICATES.subtitle} />
      <QueryState<DuplicatesResponse>
        query={dups}
        isEmpty={(d) => d.groups.length === 0 && d.pairs.length === 0}
        empty={dups.data ? DUPLICATES.empty(dups.data.threshold_pct) : null}
      >
        {(data) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <div className="report-meta">
              <Typography.Text>{DUPLICATES.settings(data.metric, data.threshold_pct)}</Typography.Text>
              <ReportSourceNote source={data.source} generatedAt={data.generated_at} stale={data.stale} />
            </div>
            <Segmented
              value={view}
              onChange={(v) => setView(v as 'groups' | 'pairs')}
              options={[
                { value: 'groups', label: `${DUPLICATES.groups} (${data.groups.length})` },
                { value: 'pairs', label: `${DUPLICATES.pairs} (${data.pairs.length})` },
              ]}
            />
            {view === 'groups' ? (
              data.groups.map((g) => <GroupCard key={g.group_id} run={runId} group={g} />)
            ) : (
              <PairsTable run={runId} pairs={data.pairs} />
            )}
          </Space>
        )}
      </QueryState>
    </>
  );
}
