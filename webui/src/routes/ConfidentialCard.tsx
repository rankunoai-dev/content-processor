import { LockOutlined } from '@ant-design/icons';
import { Alert, Card, Space, Switch, Tag, Typography } from 'antd';
import { useState } from 'react';
import { ApiError } from '../api/client';
import { projectsApi, useProjectChange, type Project } from '../api/projects';
import { PROJECTS } from '../vocabulary';

/**
 * The project's Confidential option (Settings tab): an editable switch that needs
 * the project unlocked, like every other change. Confidential: page text from the
 * project never goes to Claude or Claude Desktop; only counts and names.
 */
export function ConfidentialCard({ project, token }: { project: Project; token: string | null }) {
  const [error, setError] = useState<string | null>(null);
  const change = useProjectChange(project.project_id, (confidential: boolean) =>
    projectsApi.update(project.project_id, { confidential }, token),
  );
  const unlocked = token !== null;
  const text = PROJECTS.confidential;
  return (
    <Card
      title={
        <Space>
          <LockOutlined />
          {text.title}
          {project.confidential ? <Tag color="gold">{text.tag}</Tag> : null}
        </Space>
      }
    >
      <Space align="start" size={12}>
        <Switch
          aria-label={text.label}
          checked={project.confidential}
          disabled={!unlocked}
          loading={change.isPending}
          onChange={(checked) => {
            setError(null);
            change.mutate(checked, {
              onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
            });
          }}
        />
        <div>
          <Typography.Text strong>{text.label}</Typography.Text>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>
            {text.help}
          </Typography.Paragraph>
          <Typography.Text type="secondary">{project.confidential ? text.onHelp : text.offHelp}</Typography.Text>
        </div>
      </Space>
      {!unlocked ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          {PROJECTS.lock.needed}
        </Typography.Paragraph>
      ) : null}
      {error ? <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} /> : null}
    </Card>
  );
}
