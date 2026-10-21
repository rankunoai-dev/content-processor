import {
  DownloadOutlined,
  FileExcelOutlined,
  FileOutlined,
  FileTextOutlined,
  GlobalOutlined,
} from '@ant-design/icons';
import { Button, Card, List, Space, Tag, Typography } from 'antd';
import { useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { DownloadsResponse } from '../api/types';
import { useDownloads } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { PageHeading } from '../components/bits';
import { DOWNLOADS, describeFile, formatBytes, formatDate } from '../vocabulary';

function FileIcon({ name }: { name: string }) {
  if (/\.(xlsx|csv)$/.test(name)) return <FileExcelOutlined className="file-icon file-icon-sheet" />;
  if (/\.html$/.test(name)) return <GlobalOutlined className="file-icon" />;
  if (/\.(jsonl?|txt)$/.test(name)) return <FileTextOutlined className="file-icon" />;
  return <FileOutlined className="file-icon" />;
}

/** Spreadsheets first: they are what analysts open. */
function rank(name: string): number {
  if (name.endsWith('.xlsx')) return 0;
  if (name.endsWith('.csv')) return 1;
  if (name.endsWith('.html')) return 3;
  if (name === 'manifest.json' || name.endsWith('_manifest.json')) return 4;
  return 2;
}

export default function DownloadsPage() {
  const { runId = '' } = useParams();
  const query = useDownloads(runId);
  return (
    <>
      <PageHeading title={DOWNLOADS.title} subtitle={DOWNLOADS.subtitle} />
      <QueryState<DownloadsResponse> query={query} isEmpty={(d) => d.files.length === 0} empty={DOWNLOADS.empty}>
        {(data) => (
          <Card className="table-card">
            <List
              itemLayout="horizontal"
              dataSource={[...data.files].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name))}
              renderItem={(file) => {
                const info = describeFile(file.name);
                return (
                  <List.Item
                    key={file.name}
                    actions={[
                      <Button
                        key="dl"
                        type={rank(file.name) === 0 ? 'primary' : 'default'}
                        icon={<DownloadOutlined />}
                        href={api.downloadUrl(runId, file.name)}
                        download={file.name}
                        aria-label={`${DOWNLOADS.download} ${file.name}`}
                      >
                        {DOWNLOADS.download}
                      </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={<FileIcon name={file.name} />}
                      title={
                        <Space size={8} wrap>
                          <Typography.Text strong>{info.title}</Typography.Text>
                          <Tag bordered={false}>{info.kind}</Tag>
                        </Space>
                      }
                      description={
                        <Space direction="vertical" size={2}>
                          <Typography.Text type="secondary">{info.text}</Typography.Text>
                          <Typography.Text type="secondary" className="small-note">
                            {file.name} · {formatBytes(file.size)} · {DOWNLOADS.updated} {formatDate(file.modified_at, true)}
                          </Typography.Text>
                        </Space>
                      }
                    />
                  </List.Item>
                );
              }}
            />
          </Card>
        )}
      </QueryState>
    </>
  );
}
