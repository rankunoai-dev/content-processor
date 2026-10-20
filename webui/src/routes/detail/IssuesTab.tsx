import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import { Button, Collapse, Result, Space, Tag, Tooltip, Typography } from 'antd';
import { useState } from 'react';
import type { PageDetail } from '../../api/types';
import { TechName } from '../../components/bits';
import {
  DETAIL,
  ISSUE_ORDER,
  describeFinding,
  groupGaps,
  issueInfo,
  type IssueInfo,
} from '../../vocabulary';

const LABELS_SHOWN = 12;

interface Cause {
  key: string;
  sentence: string;
  labels: string[];
  count: number;
}

/** One card per issue type; each lists its causes in plain sentences. */
interface IssueCard {
  issue: string;
  causes: Cause[];
  count: number;
  retryable: boolean;
  raw: string[];
}

function issueCards(detail: PageDetail): IssueCard[] {
  const byIssue = new Map<string, IssueCard>();
  const card = (issue: string): IssueCard => {
    let c = byIssue.get(issue);
    if (!c) {
      c = { issue, causes: [], count: 0, retryable: false, raw: [] };
      byIssue.set(issue, c);
    }
    return c;
  };
  for (const g of groupGaps(detail.gaps)) {
    const c = card(g.gapType);
    c.causes.push({ key: g.key, sentence: g.sentence, labels: g.labels, count: g.gaps.length });
    c.count += g.gaps.length;
    c.retryable ||= g.retryable;
    c.raw.push(...g.gaps.map((x) => x.reason));
  }
  for (const f of detail.findings) {
    const c = card(f.key);
    c.causes.push({ key: `finding-${f.key}`, sentence: describeFinding(f), labels: [], count: 1 });
    c.count += 1;
    c.raw.push(`${f.key}: ${f.unconfirmed}/${f.total}`);
  }
  const rank = (k: string) => {
    const i = ISSUE_ORDER.indexOf(k);
    return i === -1 ? ISSUE_ORDER.length : i;
  };
  return [...byIssue.values()].sort((a, b) => rank(a.issue) - rank(b.issue) || b.count - a.count);
}

function LevelIcon({ level }: { level: IssueInfo['level'] }) {
  if (level === 'error') return <CloseCircleOutlined className="issue-icon issue-icon-error" />;
  if (level === 'warning') return <ExclamationCircleOutlined className="issue-icon issue-icon-warning" />;
  return <InfoCircleOutlined className="issue-icon issue-icon-info" />;
}

function Labels({ labels }: { labels: string[] }) {
  const [all, setAll] = useState(false);
  if (labels.length < 2) return null;
  const shown = all ? labels : labels.slice(0, LABELS_SHOWN);
  return (
    <div className="issue-labels">
      {shown.map((l) => (
        <Tag key={l} bordered={false} className="label-tag">
          {l}
        </Tag>
      ))}
      {labels.length > LABELS_SHOWN ? (
        <Button type="link" size="small" onClick={() => setAll(!all)}>
          {all ? DETAIL.showFewer : DETAIL.showAll(labels.length)}
        </Button>
      ) : null}
    </div>
  );
}

export function IssuesTab({ detail }: { detail: PageDetail }) {
  const list = issueCards(detail);
  if (list.length === 0) {
    return <Result icon={<CheckCircleOutlined style={{ color: '#389e0d' }} />} title={DETAIL.noIssues} />;
  }
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }} className="issues-tab">
      {list.map((card) => {
        const info = issueInfo(card.issue);
        return (
          <section key={card.issue} className={`issue-card issue-card-${info.level}`} aria-label={info.title}>
            <div className="issue-card-head">
              <LevelIcon level={info.level} />
              <Typography.Text strong className="issue-title">
                {info.title}
              </Typography.Text>
              <TechName name={card.issue} />
              {card.count > 1 ? <Tag bordered={false}>{card.count}</Tag> : null}
              {card.retryable ? (
                <Tooltip title={DETAIL.canRetryHelp}>
                  <Tag color="green" bordered={false}>
                    {DETAIL.canRetry}
                  </Tag>
                </Tooltip>
              ) : null}
            </div>
            <ul className="issue-causes">
              {card.causes.map((cause) => (
                <li key={cause.key}>
                  <Typography.Text>{cause.sentence}</Typography.Text>
                  <Labels labels={cause.labels} />
                </li>
              ))}
            </ul>
            <Typography.Paragraph className="issue-todo">
              <Typography.Text strong>{DETAIL.whatToDo}: </Typography.Text>
              {info.todo}
            </Typography.Paragraph>
            <Collapse
              ghost
              size="small"
              items={[
                {
                  key: 'tech',
                  label: DETAIL.technicalDetails,
                  children: <pre className="raw-reasons">{card.raw.slice(0, 200).join('\n')}</pre>,
                },
              ]}
            />
          </section>
        );
      })}
    </Space>
  );
}
