import {
  FolderOpenOutlined,
  GlobalOutlined,
  MinusCircleOutlined,
  PlusOutlined,
  RightOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Alert, Badge, Button, Card, Col, Divider, Empty, Form, Input, Modal, Row, Space, Tag, Typography } from 'antd';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/client';
import { ACTIVE, projectKeys, projectsApi, useProjectTokens, useProjects, type Project } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { StatusBar } from '../components/StatusBar';
import { PageHeading } from '../components/bits';
import { PROJECTS, formatDate } from '../vocabulary';

interface CreateForm {
  name: string;
  owner_name: string;
  client_name: string;
  password: string;
  confirm: string;
  client_website?: string;
  competitors?: { name?: string; website?: string }[];
}

function CreateProject({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [form] = Form.useForm<CreateForm>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const remember = useProjectTokens((s) => s.remember);
  const client = useQueryClient();
  const navigate = useNavigate();

  const submit = async (values: CreateForm) => {
    setBusy(true);
    setError(null);
    try {
      const created = await projectsApi.create({
        name: values.name.trim(),
        owner_name: values.owner_name.trim(),
        client_name: values.client_name.trim(),
        password: values.password,
        client_website: values.client_website?.trim() || null,
        competitors: (values.competitors ?? [])
          .filter((c) => c.name?.trim())
          .map((c) => ({ name: (c.name ?? '').trim(), website: c.website?.trim() || null })),
      });
      remember(created.project.project_id, created.token, created.expires_at);
      await client.invalidateQueries({ queryKey: projectKeys.all() });
      form.resetFields();
      onClose();
      navigate(`/projects/${encodeURIComponent(created.project.project_id)}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={PROJECTS.form.title}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={PROJECTS.form.create}
      confirmLoading={busy}
      destroyOnClose
      width={640}
    >
      <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item
          name="name"
          label={PROJECTS.form.name}
          rules={[{ required: true, whitespace: true, message: PROJECTS.form.required }]}
        >
          <Input placeholder={PROJECTS.form.namePlaceholder} maxLength={120} autoFocus />
        </Form.Item>
        <Form.Item
          name="owner_name"
          label={PROJECTS.form.owner}
          rules={[{ required: true, whitespace: true, message: PROJECTS.form.required }]}
        >
          <Input maxLength={120} autoComplete="name" />
        </Form.Item>
        <Form.Item
          name="client_name"
          label={PROJECTS.form.client}
          rules={[{ required: true, whitespace: true, message: PROJECTS.form.required }]}
        >
          <Input maxLength={120} autoComplete="organization" />
        </Form.Item>
        <Form.Item name="client_website" label={PROJECTS.sites.clientWebsite}>
          <Input maxLength={500} placeholder={PROJECTS.sites.clientWebsitePlaceholder} />
        </Form.Item>
        <Divider orientation="left" orientationMargin={0} plain>
          {PROJECTS.sites.competitors}
        </Divider>
        <Typography.Paragraph type="secondary">{PROJECTS.sites.competitorsHelp}</Typography.Paragraph>
        <Form.List name="competitors">
          {(fields, { add, remove }) => (
            <>
              {fields.map((field, index) => (
                <Space key={field.key} align="baseline" wrap style={{ display: 'flex' }}>
                  <Form.Item
                    name={[field.name, 'name']}
                    rules={[{ required: true, whitespace: true, message: PROJECTS.form.required }]}
                  >
                    <Input
                      aria-label={`${PROJECTS.sites.competitorName} ${index + 1}`}
                      placeholder={PROJECTS.sites.competitorName}
                      maxLength={120}
                    />
                  </Form.Item>
                  <Form.Item name={[field.name, 'website']}>
                    <Input
                      aria-label={`${PROJECTS.sites.competitorWebsite} ${index + 1}`}
                      placeholder={PROJECTS.sites.competitorWebsite}
                      maxLength={500}
                    />
                  </Form.Item>
                  <Button
                    type="text"
                    icon={<MinusCircleOutlined />}
                    aria-label={`${PROJECTS.sites.remove} ${index + 1}`}
                    onClick={() => remove(field.name)}
                  />
                </Space>
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({})} block>
                {PROJECTS.sites.addCompetitor}
              </Button>
            </>
          )}
        </Form.List>
        <Divider />
        <Form.Item
          name="password"
          label={PROJECTS.form.password}
          extra={PROJECTS.form.passwordHelp}
          rules={[
            { required: true, message: PROJECTS.form.required },
            { min: 8, message: PROJECTS.form.tooShort },
          ]}
        >
          <Input.Password maxLength={200} autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label={PROJECTS.form.confirm}
          dependencies={['password']}
          rules={[
            { required: true, message: PROJECTS.form.required },
            ({ getFieldValue }) => ({
              validator: (_, value) =>
                !value || getFieldValue('password') === value
                  ? Promise.resolve()
                  : Promise.reject(new Error(PROJECTS.form.mismatch)),
            }),
          ]}
        >
          <Input.Password maxLength={200} autoComplete="new-password" />
        </Form.Item>
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </Form>
    </Modal>
  );
}

function ProjectCard({ project }: { project: Project }) {
  const href = `/projects/${encodeURIComponent(project.project_id)}`;
  const run = project.last_run;
  return (
    <Card
      className="run-card"
      hoverable
      title={
        <Link to={href} className="run-card-title">
          {project.name}
        </Link>
      }
      extra={
        run && ACTIVE.has(run.status) ? (
          <Badge status="processing" text={PROJECTS.status[run.status]} />
        ) : (
          <Link to={href} aria-label={`${PROJECTS.open} ${project.name}`}>
            {PROJECTS.open} <RightOutlined />
          </Link>
        )
      }
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Space size={16} wrap className="run-card-meta">
          <span>
            <FolderOpenOutlined /> {PROJECTS.client}: {project.client_name}
          </span>
          <span>
            <UserOutlined /> {PROJECTS.owner}: {project.owner_name}
          </span>
        </Space>
        <Space size={[4, 4]} wrap>
          <GlobalOutlined />
          {project.sites.map((s) => (
            <Tag key={s.site_id} color={s.role === 'client' ? 'blue' : 'default'} bordered={false}>
              {s.name}
            </Tag>
          ))}
        </Space>
        <Typography.Text type="secondary">
          {PROJECTS.sites.count(project.sites.length)} · {PROJECTS.urls(project.url_count)} ·{' '}
          {PROJECTS.created} {formatDate(project.created_at)}
        </Typography.Text>
        {run ? (
          <>
            <Typography.Text>
              {PROJECTS.status[run.status]} · {formatDate(run.created_at)} ·{' '}
              {PROJECTS.progress(run.pages_done, run.url_count)}
            </Typography.Text>
            <StatusBar badges={run.results} total={Math.max(run.url_count, 1)} />
          </>
        ) : (
          <Typography.Text type="secondary">{PROJECTS.noRunYet}</Typography.Text>
        )}
      </Space>
    </Card>
  );
}

export default function ProjectsPage() {
  const projects = useProjects();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <PageHeading
        title={PROJECTS.title}
        subtitle={PROJECTS.subtitle}
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
            {PROJECTS.newProject}
          </Button>
        }
      />
      <QueryState query={projects} rows={6}>
        {(data) =>
          data.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={PROJECTS.empty}>
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
                {PROJECTS.newProject}
              </Button>
            </Empty>
          ) : (
            <Row gutter={[16, 16]}>
              {data.map((p) => (
                <Col key={p.project_id} xs={24} lg={12} xxl={8}>
                  <ProjectCard project={p} />
                </Col>
              ))}
            </Row>
          )
        }
      </QueryState>
      <CreateProject open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
