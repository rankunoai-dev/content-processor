/**
 * The project's "Downloads" tab: every report of every module, each in Excel, CSV or
 * Word, and everything in one zip.
 */
import { DownloadOutlined, FileExcelOutlined, FileTextOutlined, FileWordOutlined } from '@ant-design/icons';
import { Alert, Button, Card, List, Space, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';
import { exportsApi, useExportCatalog, type ExportFormat } from '../api/exports';
import type { Project } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { EXPORTS } from '../vocabulary';

const ICONS: Record<ExportFormat, ReactNode> = {
  xlsx: <FileExcelOutlined />,
  csv: <FileTextOutlined />,
  docx: <FileWordOutlined />,
};

export function DownloadsTab({ project }: { project: Project }) {
  const catalog = useExportCatalog(project.project_id);
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message={EXPORTS.intro} />
      <QueryState query={catalog} rows={6}>
        {(c) => {
          const ready = c.reports.filter((r) => r.available).length;
          return (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Space wrap>
                <Button
                  type="primary"
                  icon={<DownloadOutlined />}
                  href={exportsApi.allUrl(project.project_id)}
                  disabled={ready === 0}
                >
                  {EXPORTS.all}
                </Button>
                <Typography.Text type="secondary">{EXPORTS.ready(ready, c.reports.length)}</Typography.Text>
              </Space>
              <Card size="small">
                <List
                  dataSource={c.reports}
                  renderItem={(r) => (
                    <List.Item>
                      <Space direction="vertical" size={4} style={{ width: '100%' }}>
                        <Space wrap>
                          <Typography.Text strong>{r.title}</Typography.Text>
                          <Tag>{EXPORTS.modules[r.module] ?? r.module}</Tag>
                          {r.available ? null : <Tag>{EXPORTS.notYet}</Tag>}
                        </Space>
                        <Typography.Text type="secondary">{r.description}</Typography.Text>
                        {r.available ? (
                          <>
                            <Typography.Text type="secondary">
                              {r.subtitle} · {EXPORTS.rows(r.rows)}
                            </Typography.Text>
                            <Space wrap size={6}>
                              {c.formats.map((f) => (
                                <Button
                                  key={f.key}
                                  size="small"
                                  icon={ICONS[f.key]}
                                  href={exportsApi.url(project.project_id, r.key, f.key)}
                                  aria-label={`${r.title} ${f.label}`}
                                >
                                  {f.label}
                                </Button>
                              ))}
                            </Space>
                          </>
                        ) : (
                          <Typography.Text type="warning">{r.missing}</Typography.Text>
                        )}
                      </Space>
                    </List.Item>
                  )}
                />
              </Card>
              <Typography.Text type="secondary">{EXPORTS.scrapeFiles}</Typography.Text>
            </Space>
          );
        }}
      </QueryState>
    </Space>
  );
}
