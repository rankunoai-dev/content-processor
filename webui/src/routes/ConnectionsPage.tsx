import { PlusOutlined } from '@ant-design/icons';
import { Alert, App as AntApp, Button, Card, Form, Input, Select, Space, Table, Tag, Typography } from 'antd';
import { useState } from 'react';
import { ApiError } from '../api/client';
import {
  type Connection,
  type NewConnection,
  type Platform,
  useConnections,
  useRemoveConnection,
  useSaveConnection,
} from '../api/connections';
import { CONNECTIONS } from '../vocabulary';

/**
 * Connections (admins): every saved platform account, whether its key is set, and which
 * projects use it. Adding stores only the name of the key's environment variable.
 */
export function ConnectionsPage() {
  const { message } = AntApp.useApp();
  const list = useConnections();
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const rows = list.data?.connections ?? [];
  const platforms: Platform[] = list.data?.platforms ?? ['semrush', 'ahrefs', 'gsc', 'ga4'];

  return (
    <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          {CONNECTIONS.title}
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
          {CONNECTIONS.intro}
        </Typography.Paragraph>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        <Card
          extra={
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setAdding((v) => !v)}>
              {CONNECTIONS.add}
            </Button>
          }
        >
          {adding ? (
            <AddForm
              platforms={platforms}
              onDone={() => {
                setAdding(false);
                void message.success(CONNECTIONS.saved);
              }}
              onError={setError}
            />
          ) : null}
          <Table<Connection>
            rowKey="connection_id"
            size="small"
            loading={list.isLoading}
            dataSource={rows}
            pagination={false}
            locale={{ emptyText: CONNECTIONS.empty }}
            columns={[
              {
                title: CONNECTIONS.platform,
                dataIndex: 'platform',
                render: (p: string) => CONNECTIONS.platforms[p] ?? p,
              },
              { title: CONNECTIONS.label, dataIndex: 'label' },
              {
                title: CONNECTIONS.secretEnv,
                dataIndex: 'secret_env',
                render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
              },
              {
                title: 'Key',
                dataIndex: 'key_set',
                render: (set: boolean) =>
                  set ? <Tag color="green">{CONNECTIONS.keySet}</Tag> : <Tag color="red">{CONNECTIONS.keyMissing}</Tag>,
              },
              {
                title: CONNECTIONS.usedBy,
                dataIndex: 'used_by',
                render: (names: string[]) =>
                  names.length ? names.join(', ') : <Typography.Text type="secondary">{CONNECTIONS.notUsed}</Typography.Text>,
              },
              {
                title: '',
                key: 'remove',
                render: (_: unknown, c: Connection) => (
                  <RemoveButton connection={c} onError={setError} onDone={() => void message.success(CONNECTIONS.removed)} />
                ),
              },
            ]}
          />
        </Card>
      </Space>
    </div>
  );
}

function AddForm({
  platforms,
  onDone,
  onError,
}: {
  platforms: Platform[];
  onDone: () => void;
  onError: (message: string | null) => void;
}) {
  const save = useSaveConnection();
  const [form] = Form.useForm<NewConnection>();
  return (
    <Form
      form={form}
      layout="vertical"
      requiredMark
      onFinish={(values) => {
        onError(null);
        save.mutate(
          { ...values, property_ref: values.property_ref ?? '' },
          {
            onSuccess: () => {
              form.resetFields();
              onDone();
            },
            onError: (e) => onError(e instanceof ApiError ? e.message : String(e)),
          },
        );
      }}
    >
      <Space wrap align="start">
        <Form.Item name="platform" label={CONNECTIONS.platform} rules={[{ required: true }]}>
          <Select
            style={{ width: 220 }}
            options={platforms.map((p) => ({ value: p, label: CONNECTIONS.platforms[p] ?? p }))}
          />
        </Form.Item>
        <Form.Item name="label" label={CONNECTIONS.label} help={CONNECTIONS.labelHelp} rules={[{ required: true }]}>
          <Input style={{ width: 260 }} maxLength={80} />
        </Form.Item>
        <Form.Item
          name="secret_env"
          label={CONNECTIONS.secretEnv}
          help={CONNECTIONS.secretEnvHelp}
          rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{2,63}$/, message: 'Use upper-case letters, digits and underscores.' }]}
        >
          <Input style={{ width: 260 }} placeholder="SEMRUSH_AGENCY" />
        </Form.Item>
        <Form.Item name="property_ref" label={CONNECTIONS.propertyRef} help={CONNECTIONS.propertyHelp}>
          <Input style={{ width: 240 }} maxLength={200} />
        </Form.Item>
      </Space>
      <Button type="primary" htmlType="submit" loading={save.isPending}>
        {CONNECTIONS.add}
      </Button>
    </Form>
  );
}

function RemoveButton({
  connection,
  onError,
  onDone,
}: {
  connection: Connection;
  onError: (message: string | null) => void;
  onDone: () => void;
}) {
  const remove = useRemoveConnection();
  const inUse = connection.used_by.length > 0;
  return (
    <Button
      danger
      size="small"
      disabled={inUse}
      title={inUse ? CONNECTIONS.removeBlocked : undefined}
      loading={remove.isPending}
      onClick={() => {
        onError(null);
        remove.mutate(connection.connection_id, {
          onSuccess: onDone,
          onError: (e) => onError(e instanceof ApiError ? e.message : String(e)),
        });
      }}
    >
      {CONNECTIONS.remove}
    </Button>
  );
}
