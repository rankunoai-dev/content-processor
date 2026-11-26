/**
 * The signed-in person in the header: name and role, change password, sign out, and
 * (admins) the Accounts page. Nothing is shown while there are no accounts.
 */
import {
  ApiOutlined,
  DownOutlined,
  KeyOutlined,
  LogoutOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { App as AntApp, Button, Dropdown, Modal } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { authApi, useAfterAuthChange, useMe } from '../api/auth';
import { AUTH, CONNECTIONS } from '../vocabulary';
import { PasswordForm } from './AuthGate';

export function UserMenu() {
  const me = useMe();
  const after = useAfterAuthChange();
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const [changing, setChanging] = useState(false);
  const account = me.data?.account;
  if (!me.data?.accounts || !account) return null;
  return (
    <>
      <Dropdown
        trigger={['click']}
        menu={{
          items: [
            { key: 'who', label: AUTH.signedInAs(account.username, AUTH.roles[account.role] ?? account.role), disabled: true },
            { type: 'divider' },
            ...(account.role === 'admin'
              ? [
                  { key: 'accounts', icon: <TeamOutlined />, label: AUTH.accountsNav },
                  { key: 'connections', icon: <ApiOutlined />, label: CONNECTIONS.title },
                ]
              : []),
            { key: 'password', icon: <KeyOutlined />, label: AUTH.changePassword },
            { key: 'out', icon: <LogoutOutlined />, label: AUTH.signOut },
          ],
          onClick: ({ key }) => {
            if (key === 'accounts') navigate('/accounts');
            if (key === 'connections') navigate('/connections');
            if (key === 'password') setChanging(true);
            if (key === 'out') {
              void authApi.logout().finally(() => after({ ...me.data, account: null }));
            }
          },
        }}
      >
        <Button type="text" icon={<UserOutlined />} aria-label={AUTH.userMenu}>
          {account.display_name} <DownOutlined />
        </Button>
      </Dropdown>
      <Modal title={AUTH.changePassword} open={changing} onCancel={() => setChanging(false)} footer={null} destroyOnClose>
        <PasswordForm
          minLength={me.data.min_password}
          onDone={() => {
            setChanging(false);
            void message.success(AUTH.passwordSaved);
          }}
        />
      </Modal>
    </>
  );
}
