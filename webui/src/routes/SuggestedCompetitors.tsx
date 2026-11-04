/**
 * Suggested competitors in the Research tab: the websites the search data shows
 * competing with the client, with what shows it; add one to the project or dismiss it.
 */
import { PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { App as AntApp, Button, Card, Space, Switch, Table, Tag, Typography, Upload } from 'antd';
import { useState } from 'react';
import {
  useCompetitorBook,
  useDecideCompetitor,
  useImportCompetitors,
  type CompetitorSuggestion,
} from '../api/research';
import { DownloadMenu } from '../components/DownloadMenu';
import { QueryState } from '../components/QueryState';
import { COMPETITORS } from '../vocabulary';

type Action = 'add' | 'dismiss' | 'restore';

function Actions({ s, projectId, token }: { s: CompetitorSuggestion; projectId: string; token: string | null }) {
  const decide = useDecideCompetitor(projectId);
  const { message } = AntApp.useApp();
  const run = (action: Action) =>
    decide.mutate(
      { domain: s.domain, action, token },
      {
        onSuccess: () => {
          if (action === 'add') void message.success(COMPETITORS.added(s.domain));
        },
        onError: (e) => void message.error(e.message),
      },
    );
  if (s.status === 'added') return <Tag color="green">{COMPETITORS.inProject}</Tag>;
  if (s.status === 'dismissed')
    return (
      <Button size="small" disabled={token === null} loading={decide.isPending} onClick={() => run('restore')}>
        {COMPETITORS.restore}
      </Button>
    );
  return (
    <Space wrap size={4}>
      <Button
        size="small"
        type="primary"
        icon={<PlusOutlined />}
        disabled={token === null}
        loading={decide.isPending && decide.variables?.action === 'add'}
        onClick={() => run('add')}
      >
        {COMPETITORS.add}
      </Button>
      <Button size="small" disabled={token === null} onClick={() => run('dismiss')}>
        {COMPETITORS.dismiss}
      </Button>
    </Space>
  );
}

export function SuggestedCompetitors({ projectId, token }: { projectId: string; token: string | null }) {
  const book = useCompetitorBook(projectId);
  const upload = useImportCompetitors(projectId);
  const [showDismissed, setShowDismissed] = useState(false);
  const { message } = AntApp.useApp();
  return (
    <Card
      size="small"
      title={COMPETITORS.title}
      extra={
        <Space wrap size={6}>
          <DownloadMenu projectId={projectId} reportKey="competitors" size="small" />
          <Upload
          accept=".csv,text/csv"
          showUploadList={false}
          disabled={token === null}
          beforeUpload={(file) => {
            upload.mutate(
              { file, token },
              {
                onSuccess: (b) =>
                  void message.success(
                    COMPETITORS.imported(b.suggestions.filter((s) => s.status === 'suggested').length),
                  ),
                onError: (e) => void message.error(e.message),
              },
            );
            return false;
          }}
        >
          <Button size="small" icon={<UploadOutlined />} loading={upload.isPending} disabled={token === null}>
            {COMPETITORS.importExport}
          </Button>
          </Upload>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{COMPETITORS.help}</Typography.Text>
        <QueryState query={book} rows={3}>
          {(b) => {
            if (b.suggestions.length === 0)
              return <Typography.Text type="secondary">{COMPETITORS.empty}</Typography.Text>;
            const dismissed = b.suggestions.filter((s) => s.status === 'dismissed').length;
            const shown = b.suggestions.filter((s) => showDismissed || s.status !== 'dismissed');
            return (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap>
                  {b.client_domain ? <Tag>{COMPETITORS.client(b.client_domain)}</Tag> : null}
                  {dismissed > 0 ? (
                    <Space size={6}>
                      <Switch
                        size="small"
                        checked={showDismissed}
                        onChange={setShowDismissed}
                        aria-label={COMPETITORS.showDismissed}
                      />
                      <Typography.Text type="secondary">
                        {COMPETITORS.showDismissed} ({dismissed})
                      </Typography.Text>
                    </Space>
                  ) : null}
                </Space>
                <Table<CompetitorSuggestion>
                  rowKey="domain"
                  size="small"
                  scroll={{ x: true }}
                  pagination={{ pageSize: 15, hideOnSinglePage: true }}
                  dataSource={shown}
                  columns={[
                    {
                      title: COMPETITORS.columns.website,
                      render: (_v, s) => (
                        <a href={`https://${s.domain}/`} target="_blank" rel="noreferrer noopener">
                          {s.domain}
                        </a>
                      ),
                    },
                    {
                      title: COMPETITORS.columns.foundBy,
                      render: (_v, s) => s.sources.map((x) => <Tag key={x}>{COMPETITORS.sources[x] ?? x}</Tag>),
                    },
                    {
                      title: COMPETITORS.columns.relevance,
                      sorter: (a, z) => (a.relevance ?? 0) - (z.relevance ?? 0),
                      render: (_v, s) => (s.relevance === null ? '-' : `${Math.round(s.relevance * 100)}%`),
                    },
                    {
                      title: COMPETITORS.columns.common,
                      sorter: (a, z) => a.common_keywords - z.common_keywords,
                      render: (_v, s) => (s.common_keywords ? s.common_keywords.toLocaleString() : '-'),
                    },
                    {
                      title: COMPETITORS.columns.traffic,
                      render: (_v, s) => (s.organic_traffic ? s.organic_traffic.toLocaleString() : '-'),
                    },
                    {
                      title: COMPETITORS.columns.top10,
                      render: (_v, s) =>
                        s.search_results ? (
                          <span title={s.example_keywords.join(', ')}>
                            {COMPETITORS.keywordsCount(s.search_results)}
                          </span>
                        ) : (
                          '-'
                        ),
                    },
                    {
                      title: COMPETITORS.columns.action,
                      render: (_v, s) => <Actions s={s} projectId={projectId} token={token} />,
                    },
                  ]}
                />
              </Space>
            );
          }}
        </QueryState>
      </Space>
    </Card>
  );
}
