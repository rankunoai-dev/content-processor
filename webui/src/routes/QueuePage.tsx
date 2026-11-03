/**
 * The shared scrape queue: what is being scraped now, who waits and in which order,
 * and whose turn came. One scrape runs at a time for every project and person.
 */
import { Card, Empty, Progress, Space, Table, Tag, Typography } from 'antd';
import { Link } from 'react-router-dom';
import { useSharedQueue, type QueueItem } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { PageHeading } from '../components/bits';
import { QUEUE, formatDate, roughDuration } from '../vocabulary';

function projectLink(item: QueueItem) {
  const r = item.run;
  return (
    <Link to={`/projects/${encodeURIComponent(r.project_id)}?site=${encodeURIComponent(r.site_id)}&view=scrape`}>
      {item.project_name}
    </Link>
  );
}

function whenTurn(item: QueueItem) {
  const r = item.run;
  return (
    <Space size={4} wrap>
      <Tag bordered={false}>{r.on_turn === 'notify' ? QUEUE.notify(r.notify_to) : QUEUE.auto}</Tag>
      {r.email_when_done ? <Tag bordered={false}>{QUEUE.doneMail(r.notify_to)}</Tag> : null}
    </Space>
  );
}

export default function QueuePage() {
  const queue = useSharedQueue();
  return (
    <>
      <PageHeading title={QUEUE.title} subtitle={QUEUE.subtitle} />
      <QueryState query={queue} rows={6}>
        {(data) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Card title={QUEUE.running}>
              {data.running.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={QUEUE.nothingRunning} />
              ) : (
                data.running.map((item) => (
                  <Space key={item.run.run_id} direction="vertical" style={{ width: '100%' }}>
                    <Space wrap>
                      <Typography.Text strong>{item.site_name}</Typography.Text>
                      {projectLink(item)}
                      <Typography.Text type="secondary">
                        {QUEUE.startedBy} {item.run.started_by}
                      </Typography.Text>
                    </Space>
                    <Progress
                      percent={item.run.url_count ? Math.round((100 * item.run.pages_done) / item.run.url_count) : 0}
                      status="active"
                    />
                    <Typography.Text type="secondary">
                      {QUEUE.progress(item.run.pages_done, item.run.url_count)} · {item.run.message}
                    </Typography.Text>
                  </Space>
                ))
              )}
            </Card>
            <Card title={`${QUEUE.waiting} (${data.waiting.length})`}>
              <Table
                rowKey={(i: QueueItem) => i.run.run_id}
                size="small"
                pagination={false}
                dataSource={data.waiting}
                locale={{ emptyText: QUEUE.nobodyWaiting }}
                scroll={{ x: true }}
                columns={[
                  { title: QUEUE.position, dataIndex: 'position', width: 70 },
                  { title: QUEUE.website, render: (_, i: QueueItem) => i.site_name },
                  { title: QUEUE.project, render: (_, i: QueueItem) => projectLink(i) },
                  { title: QUEUE.startedBy, render: (_, i: QueueItem) => i.run.started_by, responsive: ['md'] },
                  { title: QUEUE.addresses, render: (_, i: QueueItem) => i.run.url_count.toLocaleString() },
                  { title: QUEUE.wait, render: (_, i: QueueItem) => roughDuration(i.wait_s) },
                  { title: QUEUE.whenTurn, render: (_, i: QueueItem) => whenTurn(i) },
                ]}
              />
              <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
                {QUEUE.estimate(data.seconds_per_url)}
              </Typography.Paragraph>
            </Card>
            {data.ready.length > 0 ? (
              <Card title={QUEUE.ready}>
                <Table
                  rowKey={(i: QueueItem) => i.run.run_id}
                  size="small"
                  pagination={false}
                  dataSource={data.ready}
                  columns={[
                    { title: QUEUE.website, render: (_, i: QueueItem) => i.site_name },
                    { title: QUEUE.project, render: (_, i: QueueItem) => projectLink(i) },
                    { title: QUEUE.startedBy, render: (_, i: QueueItem) => i.run.started_by },
                    {
                      title: QUEUE.turnSince,
                      render: (_, i: QueueItem) => (i.run.turn_at ? formatDate(i.run.turn_at) : ''),
                    },
                  ]}
                />
              </Card>
            ) : null}
          </Space>
        )}
      </QueryState>
    </>
  );
}
