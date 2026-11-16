/**
 * Accounts (admins): everyone who can sign in, their role, and adding, changing,
 * disabling or resetting an account. A new or reset password is temporary: the person
 * chooses their own at their next sign-in.
 */
import { PlusOutlined } from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Result,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import { useState } from 'react';
import {
  ROLE_ORDER,
  authApi,
  useAccountChange,
  useAccounts,
  useMe,
  type Account,
  type NewAccount,
  type Role,
} from '../api/auth';
import { QueryState } from '../components/QueryState';
import { AUTH, formatDate } from '../vocabulary';

const roleOptions = ROLE_ORDER.map((r) => ({ value: r, label: AUTH.roles[r] }));

function AddAccount({ open, onClose, minLength }: { open: boolean; onClose: () => void; minLength: number }) {
  const [form] = Form.useForm<NewAccount>();
  const add = useAccountChange((body: NewAccount) => authApi.addAccount(body));
  const { message } = AntApp.useApp();
  return (
    <Modal
      title={AUTH.addTitle}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={AUTH.add}
      confirmLoading={add.isPending}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">{AUTH.addHelp}</Typography.Paragraph>
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ role: 'viewer' }}
        onFinish={(v) =>
          add.mutate(
            { ...v, username: v.username.trim().toLowerCase() },
            {
              onSuccess: (a) => {
                void message.success(AUTH.added(a.username));
                form.resetFields();
                onClose();
              },
            },
          )
        }
      >
        <Form.Item name="display_name" label={AUTH.name} rules={[{ required: true, whitespace: true, message: AUTH.required }]}>
          <Input maxLength={120} />
        </Form.Item>
        <Form.Item
          name="username"
          label={AUTH.username}
          extra={AUTH.usernameRule}
          rules={[
            { required: true, message: AUTH.required },
            { pattern: /^[a-zA-Z0-9][a-zA-Z0-9._-]{1,39}$/, message: AUTH.usernameRule },
          ]}
        >
          <Input maxLength={40} autoComplete="off" />
        </Form.Item>
        <Form.Item name="role" label={AUTH.role} extra={AUTH.roleHelp}>
          <Select options={roleOptions} />
        </Form.Item>
        <Form.Item
          name="password"
          label={AUTH.tempPassword}
          extra={AUTH.passwordRule(minLength)}
          rules={[
            { required: true, message: AUTH.required },
            { min: minLength, message: AUTH.passwordRule(minLength) },
          ]}
        >
          <Input.Password autoComplete="new-password" maxLength={200} />
        </Form.Item>
        {add.error ? <Alert type="error" showIcon message={add.error.message} /> : null}
      </Form>
    </Modal>
  );
}

function ResetPassword({ account, onClose, minLength }: { account: Account | null; onClose: () => void; minLength: number }) {
  const [form] = Form.useForm<{ password: string }>();
  const reset = useAccountChange((a: { username: string; password: string }) =>
    authApi.resetPassword(a.username, a.password),
  );
  const { message } = AntApp.useApp();
  return (
    <Modal
      title={account ? AUTH.resetTitle(account.display_name) : ''}
      open={account !== null}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={AUTH.reset}
      confirmLoading={reset.isPending}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">{AUTH.resetHelp}</Typography.Paragraph>
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={(v) =>
          account &&
          reset.mutate(
            { username: account.username, password: v.password },
            {
              onSuccess: () => {
                void message.success(AUTH.resetDone);
                onClose();
              },
            },
          )
        }
      >
        <Form.Item
          name="password"
          label={AUTH.tempPassword}
          extra={AUTH.passwordRule(minLength)}
          rules={[
            { required: true, message: AUTH.required },
            { min: minLength, message: AUTH.passwordRule(minLength) },
          ]}
        >
          <Input.Password autoComplete="new-password" maxLength={200} />
        </Form.Item>
        {reset.error ? <Alert type="error" showIcon message={reset.error.message} /> : null}
      </Form>
    </Modal>
  );
}

export function AccountsPage() {
  const me = useMe();
  const isAdmin = me.data?.account?.role === 'admin';
  const accounts = useAccounts(isAdmin);
  const change = useAccountChange((a: { username: string; body: { role?: Role; disabled?: boolean } }) =>
    authApi.updateAccount(a.username, a.body),
  );
  const { message } = AntApp.useApp();
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<Account | null>(null);
  const minLength = me.data?.min_password ?? 10;
  if (!me.data?.accounts || !isAdmin) {
    return <Result status="403" title={AUTH.adminsOnly} />;
  }
  const save = (username: string, body: { role?: Role; disabled?: boolean }) =>
    change.mutate({ username, body }, { onError: (e) => void message.error(e.message) });
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        {AUTH.accountsNav}
      </Typography.Title>
      <Alert type="info" showIcon message={AUTH.accountsIntro} />
      <Card
        size="small"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setAdding(true)}>
            {AUTH.addTitle}
          </Button>
        }
      >
        <QueryState query={accounts} rows={4}>
          {(list) => (
            <Table<Account>
              rowKey="username"
              size="small"
              scroll={{ x: true }}
              pagination={false}
              dataSource={list}
              columns={[
                {
                  title: AUTH.name,
                  render: (_v, a) => (
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>{a.display_name}</Typography.Text>
                      <Typography.Text type="secondary">{a.username}</Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: AUTH.role,
                  render: (_v, a) => (
                    <Select<Role>
                      size="small"
                      value={a.role}
                      options={roleOptions}
                      style={{ minWidth: 120 }}
                      aria-label={`${AUTH.role}: ${a.username}`}
                      onChange={(role) => save(a.username, { role })}
                    />
                  ),
                },
                {
                  title: AUTH.status,
                  render: (_v, a) =>
                    a.disabled ? (
                      <Tag>{AUTH.disabled}</Tag>
                    ) : a.must_change_password ? (
                      <Tag color="gold">{AUTH.waitingFirstSignIn}</Tag>
                    ) : (
                      <Tag color="green">{AUTH.active}</Tag>
                    ),
                },
                {
                  title: AUTH.lastSignIn,
                  render: (_v, a) => (a.last_login_at ? formatDate(a.last_login_at) : AUTH.never),
                },
                {
                  title: AUTH.enabled,
                  render: (_v, a) => (
                    <Switch
                      size="small"
                      checked={!a.disabled}
                      disabled={a.username === me.data?.account?.username}
                      aria-label={`${AUTH.enabled}: ${a.username}`}
                      onChange={(on) => save(a.username, { disabled: !on })}
                    />
                  ),
                },
                {
                  title: '',
                  render: (_v, a) => (
                    <Button size="small" onClick={() => setResetting(a)}>
                      {AUTH.reset}
                    </Button>
                  ),
                },
              ]}
            />
          )}
        </QueryState>
      </Card>
      <AddAccount open={adding} onClose={() => setAdding(false)} minLength={minLength} />
      <ResetPassword account={resetting} onClose={() => setResetting(null)} minLength={minLength} />
    </Space>
  );
}
