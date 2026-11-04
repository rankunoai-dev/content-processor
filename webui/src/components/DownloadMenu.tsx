/**
 * One "Download" button for a module's report: Excel, CSV or Word.
 */
import { DownloadOutlined, DownOutlined } from '@ant-design/icons';
import { Button, Dropdown } from 'antd';
import { exportsApi, type ExportFormat } from '../api/exports';
import { EXPORTS } from '../vocabulary';

const FORMATS: ExportFormat[] = ['xlsx', 'csv', 'docx'];

export function DownloadMenu({
  projectId,
  reportKey,
  size,
}: {
  projectId: string;
  reportKey: string;
  size?: 'small' | 'middle';
}) {
  return (
    <Dropdown
      trigger={['click']}
      menu={{
        items: FORMATS.map((f) => ({
          key: f,
          label: (
            <a href={exportsApi.url(projectId, reportKey, f)} download>
              {EXPORTS.formats[f]}
            </a>
          ),
        })),
      }}
    >
      <Button size={size} icon={<DownloadOutlined />}>
        {EXPORTS.button} <DownOutlined />
      </Button>
    </Dropdown>
  );
}
