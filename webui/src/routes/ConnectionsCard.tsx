import { ApiOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Select, Space, Typography, message } from 'antd';
import { useState } from 'react';
import { ApiError } from '../api/client';
import { type Platform, useChooseConnections, useConnections } from '../api/connections';
import type { Project } from '../api/projects';
import { CONNECTIONS } from '../vocabulary';

const ORDER: Platform[] = ['semrush', 'ahrefs', 'gsc', 'ga4'];

/**
 * The project's accounts (Settings tab): one connection per platform, picked from the
 * saved ones. Choosing needs the project unlocked. A platform left on "Installation key"
 * uses the key set for the whole server, as before.
 */
export function ConnectionsCard({ project, token }: { project: Project; token: string | null }) {
  const list = useConnections();
  const save = useChooseConnections(project.project_id);
  const [error, setError] = useState<string | null>(null);
  const chosen: Record<string, string> = project.connections ?? {};
  const unlocked = token !== null;
  const connections = list.data?.connections ?? [];

  const choose = (platform: Platform, connectionId: string | undefined) => {
    setError(null);
    const next = { ...chosen };
    if (connectionId) next[platform] = connectionId;
    else delete next[platform];
    save.mutate(
      { connections: next, token },
      {
        onSuccess: () => void message.success(CONNECTIONS.chooseSaved),
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  return (
    <Card
      title={
        <Space>
          <ApiOutlined />
          {CONNECTIONS.chooseTitle}
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">{CONNECTIONS.chooseHelp}</Typography.Paragraph>
      {!unlocked ? <Alert type="info" showIcon message={CONNECTIONS.unlockFirst} /> : null}
      {error ? <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} /> : null}
      <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
        {ORDER.map((platform) => {
          const options = connections
            .filter((c) => c.platform === platform)
            .map((c) => ({
              value: c.connection_id,
              label: `${c.label}${c.key_set ? '' : ` (${CONNECTIONS.keyMissing})`}`,
            }));
          return (
            <Space key={platform} wrap>
              <Typography.Text strong style={{ minWidth: 200, display: 'inline-block' }}>
                {CONNECTIONS.platforms[platform]}
              </Typography.Text>
              <Select
                aria-label={CONNECTIONS.platforms[platform]}
                style={{ minWidth: 320 }}
                disabled={!unlocked || save.isPending}
                value={chosen[platform] ?? null}
                placeholder={CONNECTIONS.installationKey}
                allowClear
                options={options}
                onChange={(value: string | undefined) => choose(platform, value)}
                onClear={() => choose(platform, undefined)}
              />
            </Space>
          );
        })}
      </Space>
      <Button type="link" href="/connections" style={{ paddingLeft: 0, marginTop: 8 }}>
        {CONNECTIONS.title}
      </Button>
    </Card>
  );
}
