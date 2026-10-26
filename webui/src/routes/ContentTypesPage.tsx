import { Alert, Button, Card, Checkbox, Col, Row, Space, Tag, Typography, message } from 'antd';
import { useMemo, useState } from 'react';
import type { ContentType } from '../api/types';
import { useContentTypeScope, useContentTypes } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { PageHeading } from '../components/bits';

const { Paragraph, Text } = Typography;

/** Selected by default: everything the engine can extract today. */
function supportedIds(types: ContentType[]): string[] {
  return types.filter((t) => t.status === 'supported').map((t) => t.id);
}

function TypeCard({
  type,
  checked,
  onToggle,
}: {
  type: ContentType;
  checked: boolean;
  onToggle: (id: string, on: boolean) => void;
}) {
  const supported = type.status === 'supported';
  return (
    <Card size="small" className="content-type-card">
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Checkbox checked={checked} onChange={(e) => onToggle(type.id, e.target.checked)}>
          <Text strong>{type.label}</Text>
        </Checkbox>
        <Space wrap size={4}>
          {supported ? (
            <Tag color="green">Supported</Tag>
          ) : (
            <Tag color="default">Not supported yet</Tag>
          )}
          {type.extensions.map((ext) => (
            <Tag key={ext}>{ext}</Tag>
          ))}
        </Space>
        <Text type="secondary">{type.note}</Text>
      </Space>
    </Card>
  );
}

function CopyBlock({ label, value }: { label: string; value: string }) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space>
        <Text strong>{label}</Text>
        <Button
          size="small"
          onClick={() => {
            navigator.clipboard
              .writeText(value)
              .then(() => message.success('Copied'))
              .catch(() => message.error('Could not copy. Select the text and copy it by hand.'));
          }}
        >
          Copy
        </Button>
      </Space>
      <pre className="content-type-code">{value}</pre>
    </Space>
  );
}

export default function ContentTypesPage() {
  const types = useContentTypes();
  const [selected, setSelected] = useState<string[] | null>(null);

  const chosen = useMemo(() => {
    if (selected !== null) return selected;
    return types.data ? supportedIds(types.data.types) : [];
  }, [selected, types.data]);

  const scope = useContentTypeScope(chosen);
  const unsupported = useMemo(
    () =>
      (types.data?.types ?? []).filter((t) => chosen.includes(t.id) && t.status !== 'supported'),
    [types.data, chosen],
  );

  const toggle = (id: string, on: boolean) => {
    const base = selected ?? chosen;
    setSelected(on ? [...new Set([...base, id])] : base.filter((x) => x !== id));
  };

  return (
    <>
      <PageHeading
        title="Content types"
        subtitle="Choose the kinds of files to look for on a site. Web pages are always read, because that is how links to files are found. Selected file types are fetched and extracted; files of types the engine cannot read yet are counted as excluded, not dropped."
      />
      <QueryState
        query={types}
        isEmpty={(d) => d.types.length === 0}
        empty="The server did not list any content types."
      >
        {(data) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Space>
              <Button onClick={() => setSelected(supportedIds(data.types))}>
                Select all supported
              </Button>
              <Button onClick={() => setSelected([])}>Clear selection</Button>
            </Space>
            <Row gutter={[16, 16]}>
              {data.types.map((type) => (
                <Col key={type.id} xs={24} md={12} xl={8}>
                  <TypeCard
                    type={type}
                    checked={chosen.includes(type.id)}
                    onToggle={toggle}
                  />
                </Col>
              ))}
            </Row>

            {unsupported.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${unsupported.length} selected type${unsupported.length === 1 ? '' : 's'} not extracted yet`}
                description={`${unsupported.map((t) => t.label).join(', ')}. Files of these types will be counted as excluded, with the reason in the report.`}
              />
            )}

            <Card title="What to run">
              {chosen.length === 0 ? (
                <Text type="secondary">Select at least one content type to see the command.</Text>
              ) : scope.isError ? (
                <Alert type="error" message="Could not build the selection. Try again." />
              ) : scope.data ? (
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <Paragraph>
                    This page does not start crawls. Add the content-type filter to your scope
                    file, then run the command in a terminal.
                  </Paragraph>
                  <CopyBlock
                    label="Add this to the scope file"
                    value={JSON.stringify({ allowed_mime_families: scope.data.allowed_mime_families }, null, 2)}
                  />
                  <CopyBlock label="Command" value={scope.data.command} />
                </Space>
              ) : (
                <Text type="secondary">Building the selection…</Text>
              )}
            </Card>
          </Space>
        )}
      </QueryState>
    </>
  );
}
