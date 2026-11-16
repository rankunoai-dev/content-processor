/**
 * Shows the sign-in page, or the first-time password change, before the app when team
 * accounts exist; shows the app as it is when there are none.
 */
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Form, Input, Spin, Typography } from 'antd';
import { useState, type ReactNode } from 'react';
import { authApi, useAfterAuthChange, useMe } from '../api/auth';
import { ApiError } from '../api/client';
import { AUTH } from '../vocabulary';

function Centered({ children }: { children: ReactNode }) {
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 16 }}>
      <div style={{ width: '100%', maxWidth: 400 }}>{children}</div>
    </div>
  );
}

function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

function SignIn() {
  const after = useAfterAuthChange();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (v: { username: string; password: string }) => {
    setBusy(true);
    setError(null);
    try {
      after(await authApi.login(v.username.trim(), v.password));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Centered>
      <Card>
        <Typography.Title level={3} style={{ marginTop: 0 }}>
          {AUTH.signInTitle}
        </Typography.Title>
        <Typography.Paragraph type="secondary">{AUTH.signInHelp}</Typography.Paragraph>
        <Form layout="vertical" onFinish={(v) => void submit(v)} requiredMark={false}>
          <Form.Item name="username" label={AUTH.username} rules={[{ required: true, message: AUTH.required }]}>
            <Input prefix={<UserOutlined />} autoComplete="username" autoFocus maxLength={40} />
          </Form.Item>
          <Form.Item name="password" label={AUTH.password} rules={[{ required: true, message: AUTH.required }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" maxLength={200} />
          </Form.Item>
          {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} /> : null}
          <Button type="primary" htmlType="submit" block loading={busy}>
            {AUTH.signIn}
          </Button>
        </Form>
      </Card>
    </Centered>
  );
}

/** One's own password: forced at the first sign-in, or from the user menu. */
export function PasswordForm({ minLength, onDone }: { minLength: number; onDone?: () => void }) {
  const after = useAfterAuthChange();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (v: { current: string; next: string }) => {
    setBusy(true);
    setError(null);
    try {
      after(await authApi.changePassword(v.current, v.next));
      onDone?.();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Form layout="vertical" onFinish={(v) => void submit(v)} requiredMark={false}>
      <Form.Item name="current" label={AUTH.current} rules={[{ required: true, message: AUTH.required }]}>
        <Input.Password autoComplete="current-password" maxLength={200} />
      </Form.Item>
      <Form.Item
        name="next"
        label={AUTH.newPassword}
        extra={AUTH.passwordRule(minLength)}
        rules={[
          { required: true, message: AUTH.required },
          { min: minLength, message: AUTH.passwordRule(minLength) },
        ]}
      >
        <Input.Password autoComplete="new-password" maxLength={200} />
      </Form.Item>
      <Form.Item
        name="again"
        label={AUTH.again}
        dependencies={['next']}
        rules={[
          { required: true, message: AUTH.required },
          ({ getFieldValue }) => ({
            validator: (_, value) =>
              !value || getFieldValue('next') === value ? Promise.resolve() : Promise.reject(new Error(AUTH.mismatch)),
          }),
        ]}
      >
        <Input.Password autoComplete="new-password" maxLength={200} />
      </Form.Item>
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} /> : null}
      <Button type="primary" htmlType="submit" loading={busy}>
        {AUTH.savePassword}
      </Button>
    </Form>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const me = useMe();
  if (me.isPending) {
    return (
      <Centered>
        <Spin />
      </Centered>
    );
  }
  if (me.isError || !me.data) return <>{children}</>; // the app's own error screens explain
  if (!me.data.accounts) return <>{children}</>;
  const account = me.data.account;
  if (account === null) return <SignIn />;
  if (account.must_change_password) {
    return (
      <Centered>
        <Card>
          <Typography.Title level={3} style={{ marginTop: 0 }}>
            {AUTH.firstPasswordTitle(account.display_name)}
          </Typography.Title>
          <Typography.Paragraph type="secondary">{AUTH.firstPasswordHelp}</Typography.Paragraph>
          <PasswordForm minLength={me.data.min_password} />
        </Card>
      </Centered>
    );
  }
  return <>{children}</>;
}
