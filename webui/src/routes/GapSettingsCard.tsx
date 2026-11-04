/**
 * The Gaps tab's settings: which keywords count (minimum searches a month) and how high
 * a competitor must rank. Used from the next build.
 */
import { App as AntApp, Button, Card, Form, InputNumber, Select, Space, Typography } from 'antd';
import { useEffect } from 'react';
import { useGapSettings, useSetGapSettings, type GapSettings } from '../api/gaps';
import { QueryState } from '../components/QueryState';
import { GAP_SETTINGS } from '../vocabulary';

export function GapSettingsCard({ projectId, token }: { projectId: string; token: string | null }) {
  const settings = useGapSettings(projectId);
  const save = useSetGapSettings(projectId);
  const [form] = Form.useForm<GapSettings>();
  const { message } = AntApp.useApp();
  useEffect(() => {
    if (settings.data) form.setFieldsValue(settings.data);
  }, [settings.data, form]);
  return (
    <Card size="small" title={GAP_SETTINGS.title}>
      <QueryState query={settings} rows={2}>
        {() => (
          <Form
            form={form}
            layout="vertical"
            disabled={token === null}
            onFinish={(values) =>
              save.mutate(
                { body: values, token },
                {
                  onSuccess: () => void message.success(GAP_SETTINGS.saved),
                  onError: (e) => void message.error(e.message),
                },
              )
            }
          >
            <Typography.Paragraph type="secondary">{GAP_SETTINGS.help}</Typography.Paragraph>
            <Space wrap size={24} align="start">
              <Form.Item name="min_volume" label={GAP_SETTINGS.minVolume} extra={GAP_SETTINGS.minVolumeHelp}>
                <InputNumber aria-label={GAP_SETTINGS.minVolume} min={0} max={1_000_000} step={100} />
              </Form.Item>
              <Form.Item name="competitor_top" label={GAP_SETTINGS.competitorTop}>
                <Select
                  aria-label={GAP_SETTINGS.competitorTop}
                  style={{ minWidth: 160 }}
                  options={[3, 5, 10, 20].map((n) => ({ value: n, label: GAP_SETTINGS.top(n) }))}
                />
              </Form.Item>
            </Space>
            <Button htmlType="submit" loading={save.isPending}>
              {GAP_SETTINGS.save}
            </Button>
          </Form>
        )}
      </QueryState>
    </Card>
  );
}
