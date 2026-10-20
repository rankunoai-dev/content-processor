import { Alert, Button, Collapse, Descriptions, Space, Typography } from 'antd';
import { useState } from 'react';
import type { PageDetail } from '../../api/types';
import { usePageRaw } from '../../api/queries';
import { QueryState } from '../../components/QueryState';
import { DETAIL } from '../../vocabulary';

const T = DETAIL.tech;

function KeyValues({ data }: { data: Record<string, unknown> }) {
  return (
    <Descriptions size="small" bordered column={1} className="facts">
      {Object.entries(data).map(([k, v]) => (
        <Descriptions.Item key={k} label={<Typography.Text code>{k}</Typography.Text>}>
          <Typography.Text className="mono">{typeof v === 'string' ? v : JSON.stringify(v)}</Typography.Text>
        </Descriptions.Item>
      ))}
    </Descriptions>
  );
}

export function TechnicalTab({ detail, run }: { detail: PageDetail; run: string }) {
  const [wantRaw, setWantRaw] = useState(false);
  const raw = usePageRaw(run, detail.row.page_key, wantRaw);
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message={T.hint} />
      <Collapse
        defaultActiveKey={['vector']}
        items={[
          { key: 'vector', label: T.statusVector, children: <KeyValues data={detail.status_vector} /> },
          { key: 'inventory', label: T.inventory, children: <KeyValues data={detail.inventory} /> },
          { key: 'discovery', label: T.discovery, children: <KeyValues data={detail.discovery} /> },
          {
            key: 'warnings',
            label: T.warnings,
            children: <pre className="raw-reasons">{detail.warnings.join('\n') || '—'}</pre>,
          },
          {
            key: 'raw',
            label: T.raw,
            children: wantRaw ? (
              <QueryState query={raw} rows={8}>
                {(data) => <pre className="raw-json">{JSON.stringify(data, null, 2)}</pre>}
              </QueryState>
            ) : (
              <Button onClick={() => setWantRaw(true)}>{T.showRaw}</Button>
            ),
          },
        ]}
      />
    </Space>
  );
}
