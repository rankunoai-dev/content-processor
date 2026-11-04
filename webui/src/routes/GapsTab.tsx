/**
 * The project's "Gaps" tab: build the gap analysis, filter its findings, read the
 * evidence behind each one and record what the team decided.
 */
import { AimOutlined } from '@ant-design/icons';
import {
  Alert as AntAlert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Empty,
  List,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useState } from 'react';
import { useBuildGaps, useGaps, useSetStatus, type Finding, type FindingStatus, type GapReport } from '../api/gaps';
import type { Project } from '../api/projects';
import { DownloadMenu } from '../components/DownloadMenu';
import { QueryState } from '../components/QueryState';
import { GAPS } from '../vocabulary';

const STATUSES: FindingStatus[] = ['open', 'accepted', 'done', 'dismissed'];

function FindingItem({ finding, project, token }: { finding: Finding; project: Project; token: string | null }) {
  const save = useSetStatus(project.project_id);
  const { message } = AntApp.useApp();
  return (
    <List.Item>
      <Space direction="vertical" size={4} style={{ width: '100%', minWidth: 0, overflowWrap: 'anywhere' }}>
        <Space wrap>
          <Tag color={GAPS.severityColor[finding.severity]}>{GAPS.severity[finding.severity]}</Tag>
          <Tag>{GAPS.kinds[finding.kind] ?? finding.kind}</Tag>
          <Typography.Text strong>{finding.title}</Typography.Text>
          <Typography.Text type="secondary">
            {GAPS.priority} {finding.priority}
            {finding.demand > 0 ? ` · ${GAPS.demand(finding.demand)}` : ''}
          </Typography.Text>
        </Space>
        <Typography.Text type="secondary">{finding.detail}</Typography.Text>
        <Typography.Text>
          <Typography.Text strong>{GAPS.action}: </Typography.Text>
          {finding.action}
        </Typography.Text>
        {finding.evidence.length > 0 ? (
          <Typography.Paragraph style={{ marginBottom: 0 }}>
            <Typography.Text strong>{GAPS.evidence}: </Typography.Text>
            {finding.evidence.slice(0, 8).map((e) => (
              <Tag key={`${e.kind}${e.label}${e.url ?? ''}`}>
                {e.url ? (
                  <a href={e.url} target="_blank" rel="noreferrer noopener">
                    {e.label}
                  </a>
                ) : (
                  e.label
                )}
                {e.website ? ` (${e.website})` : ''}
              </Tag>
            ))}
            {finding.evidence.length > 8 ? `+${finding.evidence.length - 8}` : null}
          </Typography.Paragraph>
        ) : null}
        <Space wrap>
          <Select<FindingStatus>
            aria-label={`${GAPS.status}: ${finding.title}`}
            size="small"
            style={{ width: 140 }}
            value={finding.status}
            disabled={token === null}
            loading={save.isPending}
            options={STATUSES.map((s) => ({ value: s, label: GAPS.statuses[s] }))}
            onChange={(status) =>
              save.mutate(
                { findingId: finding.finding_id, status, note: finding.note, token },
                { onSuccess: () => void message.success(GAPS.saved), onError: (e) => void message.error(e.message) },
              )
            }
          />
          {finding.note ? <Typography.Text type="secondary">{finding.note}</Typography.Text> : null}
        </Space>
      </Space>
    </List.Item>
  );
}

function ReportView({ report, project, token }: { report: GapReport; project: Project; token: string | null }) {
  const [kind, setKind] = useState<string>('');
  const [hideDismissed, setHideDismissed] = useState(true);
  const kinds = [...new Set(report.findings.map((f) => f.kind))];
  const shown = report.findings.filter(
    (f) => (kind === '' || f.kind === kind) && !(hideDismissed && f.status === 'dismissed'),
  );
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {report.missing_inputs.length > 0 ? (
        <AntAlert type="warning" showIcon message={GAPS.missing} description={report.missing_inputs.join(' ')} />
      ) : null}
      <Space wrap>
        <Select
          aria-label={GAPS.kind}
          style={{ minWidth: 220 }}
          value={kind}
          onChange={setKind}
          options={[
            { value: '', label: GAPS.allKinds },
            ...kinds.map((k) => ({ value: k, label: `${GAPS.kinds[k] ?? k} (${report.stats[`kind_${k}`] ?? 0})` })),
          ]}
        />
        <Checkbox checked={hideDismissed} onChange={(e) => setHideDismissed(e.target.checked)}>
          {GAPS.hideDismissed}
        </Checkbox>
      </Space>
      <Card size="small">
        {shown.length === 0 ? (
          <Typography.Text type="secondary">{GAPS.none}</Typography.Text>
        ) : (
          <List
            size="small"
            dataSource={shown}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            renderItem={(f) => <FindingItem finding={f} project={project} token={token} />}
          />
        )}
      </Card>
      {report.resolved.length > 0 ? (
        <Card size="small" title={GAPS.resolved}>
          {report.resolved.map((r) => (
            <Tag key={r} color="green">
              {r}
            </Tag>
          ))}
        </Card>
      ) : null}
    </Space>
  );
}

export function GapsTab({ project, token }: { project: Project; token: string | null }) {
  const report = useGaps(project.project_id);
  const build = useBuildGaps(project.project_id);
  const { message } = AntApp.useApp();
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <AntAlert type="info" showIcon message={GAPS.intro} />
      <QueryState query={report} rows={6}>
        {(r) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Space wrap>
              <Tooltip title={token === null ? GAPS.needsUnlock : undefined}>
                <Button
                  type={r === null ? 'primary' : 'default'}
                  icon={<AimOutlined />}
                  disabled={token === null}
                  loading={build.isPending}
                  onClick={() => build.mutate(token, { onError: (e) => void message.error(e.message) })}
                >
                  {build.isPending ? GAPS.building : r === null ? GAPS.build : GAPS.rebuild}
                </Button>
              </Tooltip>
              {r !== null ? (
                <DownloadMenu projectId={project.project_id} reportKey="gaps" />
              ) : null}
              {r !== null ? (
                <Typography.Text type="secondary">
                  {GAPS.builtAt(new Date(r.created_at).toLocaleString(), r.findings.length, r.stats.high ?? 0)}
                </Typography.Text>
              ) : null}
            </Space>
            {token === null ? <Typography.Text type="secondary">{GAPS.needsUnlock}</Typography.Text> : null}
            {r === null ? <Empty description={GAPS.empty} /> : <ReportView report={r} project={project} token={token} />}
          </Space>
        )}
      </QueryState>
    </Space>
  );
}
