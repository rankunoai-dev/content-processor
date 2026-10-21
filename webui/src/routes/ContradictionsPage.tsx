import { Card, Col, Descriptions, Row, Segmented, Select, Space, Tag, Tooltip, Typography } from 'antd';
import { useMemo } from 'react';
import { useParams } from 'react-router-dom';
import type { Contradiction, ContradictionsResponse, Statement } from '../api/types';
import { useContradictions } from '../api/queries';
import { DecidedNote, ReviewerField } from '../components/Decisions';
import { QueryState } from '../components/QueryState';
import { PageHeading, PageLink, ReportSourceNote, StatusTag, TechName } from '../components/bits';
import { useDecisionSaver } from '../state/useDecisionSaver';
import { useViewParams } from '../state/viewParams';
import {
  BUCKETS,
  CONFIDENCE,
  CONTRADICTIONS,
  DECISIONS,
  SAVING,
  describeContextField,
  describeFlag,
  humanize,
} from '../vocabulary';

function StatementCard({ run, label, s }: { run: string; label: string; s: Statement }) {
  return (
    <Card size="small" className="statement-card" title={label} extra={<StatusTag badge={s.badge} />}>
      <blockquote className="quote">{s.quote}</blockquote>
      {s.heading && s.heading !== s.title ? (
        <Typography.Paragraph type="secondary" className="small-note">
          {s.heading}
        </Typography.Paragraph>
      ) : null}
      <PageLink run={run} pageKey={s.page_key} title={s.title} url={s.url} />
    </Card>
  );
}

function DecisionPicker({ run, item }: { run: string; item: Contradiction }) {
  const { save, saving, canSave } = useDecisionSaver(run);
  return (
    <Space direction="vertical" size={2}>
      <Tooltip title={canSave ? undefined : SAVING.needName}>
        <Select
          aria-label={CONTRADICTIONS.decision}
          placeholder={CONTRADICTIONS.undecided}
          value={item.reviewer_decision || undefined}
          allowClear
          disabled={saving || !canSave}
          style={{ minWidth: 200 }}
          options={Object.entries(DECISIONS).map(([value, label]) => ({ value, label }))}
          onChange={(value?: string) => void save('contradiction', item.pair_id, value ?? '')}
        />
      </Tooltip>
      <DecidedNote by={item.decided_by} at={item.decided_at} />
    </Space>
  );
}

function ContradictionCard({ run, item }: { run: string; item: Contradiction }) {
  const bucket = BUCKETS[item.bucket];
  const confidence = CONFIDENCE[item.confidence];
  return (
    <Card
      className="contradiction-card"
      title={
        <Space size={8} wrap>
          <Tooltip title={bucket?.help}>
            <Tag color={bucket?.tag} bordered={false}>
              {bucket?.label ?? humanize(item.bucket)}
            </Tag>
          </Tooltip>
          <TechName name={item.bucket} />
          {item.flags.map((f) => (
            <Typography.Text key={f}>{describeFlag(f)}</Typography.Text>
          ))}
        </Space>
      }
      extra={item.contradiction_pct !== null ? `${CONTRADICTIONS.score}: ${Math.round(item.contradiction_pct)}%` : null}
    >
      <Row gutter={16}>
        <Col xs={24} lg={12}>
          <StatementCard run={run} label={CONTRADICTIONS.statementA} s={item.a} />
        </Col>
        <Col xs={24} lg={12}>
          <StatementCard run={run} label={CONTRADICTIONS.statementB} s={item.b} />
        </Col>
      </Row>
      <Descriptions size="small" column={{ xs: 1, lg: 3 }} className="contradiction-facts">
        <Descriptions.Item label={CONTRADICTIONS.context}>
          <Space size={[4, 4]} wrap>
            {Object.entries(item.context)
              .filter(([, v]) => v !== 'unknown')
              .map(([k, v]) => {
                const d = describeContextField(k, v);
                return (
                  <Tooltip key={k} title={d.text.length > 70 ? d.text : undefined}>
                    <Tag color={d.tag} bordered={false} className="wrap-tag">
                      {d.field}: {d.text.length > 70 ? `${d.text.slice(0, 68)}…` : d.text}
                    </Tag>
                  </Tooltip>
                );
              })}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label={CONTRADICTIONS.confidence}>
          <Tooltip title={<TechName name={item.confidence_reason} />}>
            <Tag color={confidence?.tag} bordered={false}>
              {confidence?.label ?? humanize(item.confidence)}
            </Tag>
          </Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label={CONTRADICTIONS.decision}>
          <DecisionPicker run={run} item={item} />
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}

export default function ContradictionsPage() {
  const { runId = '' } = useParams();
  const query = useContradictions(runId);
  const [view, setView] = useViewParams();
  const items = query.data?.items;
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of items ?? []) c[i.bucket] = (c[i.bucket] ?? 0) + 1;
    return c;
  }, [items]);
  return (
    <>
      <PageHeading title={CONTRADICTIONS.title} subtitle={CONTRADICTIONS.subtitle} extra={<ReviewerField />} />
      <QueryState<ContradictionsResponse> query={query} isEmpty={(d) => d.items.length === 0} empty={CONTRADICTIONS.empty}>
        {(data) => {
          const shown = data.items
            .filter((i) => !view.bucket || i.bucket === view.bucket)
            .sort((a, b) => (BUCKETS[a.bucket]?.order ?? 9) - (BUCKETS[b.bucket]?.order ?? 9));
          return (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div className="report-meta">
                <Typography.Text>
                  {data.judge_status && !data.judge_status.startsWith('disabled')
                    ? CONTRADICTIONS.judgeOn
                    : CONTRADICTIONS.judgeOff}
                </Typography.Text>
                <ReportSourceNote source={data.source} generatedAt={data.generated_at} stale={data.stale} />
              </div>
              <Space size={8} wrap>
                <Typography.Text strong>{CONTRADICTIONS.filterLabel}</Typography.Text>
                <Segmented
                  value={view.bucket}
                  onChange={(v) => setView({ bucket: String(v) }, { replace: true })}
                  options={[
                    { value: '', label: `${CONTRADICTIONS.all} (${data.items.length})` },
                    ...Object.entries(BUCKETS)
                      .sort((a, b) => a[1].order - b[1].order)
                      .map(([k, b]) => ({ value: k, label: `${b.label} (${counts[k] ?? 0})` })),
                  ]}
                />
              </Space>
              {shown.length === 0 ? (
                <Typography.Text type="secondary">{CONTRADICTIONS.emptyFiltered}</Typography.Text>
              ) : (
                shown.map((item) => <ContradictionCard key={item.pair_id} run={runId} item={item} />)
              )}
            </Space>
          );
        }}
      </QueryState>
    </>
  );
}
