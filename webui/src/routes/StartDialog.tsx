/**
 * Starting a scrape: one scrape runs at a time for everyone, so a new scrape joins the
 * shared queue. The person chooses what happens when its turn comes: it starts by
 * itself, or they are emailed and start it then; and whether to be emailed when it ends.
 */
import { Alert, Checkbox, Form, Input, Modal, Radio, Space, Typography } from 'antd';
import { useState } from 'react';
import { useSharedQueue, type StartOptions } from '../api/projects';
import { QUEUE, roughDuration } from '../vocabulary';

const EMAIL_KEY = 'cp.queue.email';

function rememberedEmail(): string {
  try {
    return window.localStorage.getItem(EMAIL_KEY) ?? '';
  } catch {
    return '';
  }
}

function rememberEmail(email: string) {
  try {
    window.localStorage.setItem(EMAIL_KEY, email);
  } catch {
    // a private window: nothing to remember
  }
}

interface FormValues {
  on_turn: 'auto' | 'notify';
  email?: string;
  email_when_done?: boolean;
}

export function StartDialog({
  what,
  urlCount,
  open,
  busy,
  onClose,
  onStart,
}: {
  /** What is scraped: a website name, or 'all websites'. */
  what: string;
  /** How many addresses this adds to the queue. */
  urlCount: number;
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onStart: (options: StartOptions) => void;
}) {
  const [form] = Form.useForm<FormValues>();
  // The dialog stays mounted while closed: ask for the queue only while it is open.
  const queue = useSharedQueue(open);
  const [onTurn, setOnTurn] = useState<'auto' | 'notify'>('auto');
  const data = queue.data;
  const ahead = data ? data.running.length + data.waiting.length : 0;
  const lastWait = data
    ? data.waiting.length > 0
      ? (data.waiting[data.waiting.length - 1]?.wait_s ?? 0) +
        (data.waiting[data.waiting.length - 1]?.run.url_count ?? 0) * data.seconds_per_url
      : data.running.reduce((s, r) => s + Math.max(r.run.url_count - r.run.pages_done, 0), 0) * data.seconds_per_url
    : 0;

  const submit = (v: FormValues) => {
    const email = v.email?.trim() || null;
    if (email) rememberEmail(email);
    onStart({ on_turn: v.on_turn, email, email_when_done: Boolean(v.email_when_done) });
  };

  return (
    <Modal
      title={QUEUE.dialogTitle(what)}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={ahead > 0 ? QUEUE.queueIt : QUEUE.startNow}
      confirmLoading={busy}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        preserve={false}
        initialValues={{ on_turn: 'auto', email: rememberedEmail(), email_when_done: false }}
        onValuesChange={(changed: Partial<FormValues>) => {
          if (changed.on_turn) setOnTurn(changed.on_turn);
        }}
        onFinish={submit}
      >
        <Alert
          type={ahead > 0 ? 'info' : 'success'}
          showIcon
          style={{ marginBottom: 16 }}
          message={ahead > 0 ? QUEUE.ahead(ahead, roughDuration(lastWait)) : QUEUE.freeNow}
          description={
            data && urlCount > 0 ? (
              <Typography.Text type="secondary">{QUEUE.estimate(data.seconds_per_url)}</Typography.Text>
            ) : null
          }
        />
        <Form.Item name="on_turn" label={QUEUE.onTurn}>
          <Radio.Group>
            <Space direction="vertical">
              <Radio value="auto">
                {QUEUE.onTurnAuto}
                <Typography.Text type="secondary"> · {QUEUE.onTurnAutoHelp}</Typography.Text>
              </Radio>
              <Radio value="notify">
                {QUEUE.onTurnNotify}
                <Typography.Text type="secondary"> · {QUEUE.onTurnNotifyHelp(data?.ready_hours ?? 72)}</Typography.Text>
              </Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        <Form.Item
          name="email"
          label={QUEUE.email}
          dependencies={['on_turn', 'email_when_done']}
          rules={[
            ({ getFieldValue }) => ({
              validator: (_, value?: string) =>
                (getFieldValue('on_turn') === 'notify' || getFieldValue('email_when_done')) && !value?.trim()
                  ? Promise.reject(new Error(QUEUE.emailRequired))
                  : Promise.resolve(),
            }),
            { type: 'email', message: QUEUE.emailRequired },
          ]}
        >
          <Input type="email" maxLength={254} autoComplete="email" placeholder={QUEUE.emailPlaceholder} />
        </Form.Item>
        <Form.Item name="email_when_done" valuePropName="checked">
          <Checkbox>{QUEUE.emailWhenDone}</Checkbox>
        </Form.Item>
        {data && !data.email_sends && onTurn === 'notify' ? (
          <Alert type="warning" showIcon message={QUEUE.emailNotSent} />
        ) : null}
      </Form>
    </Modal>
  );
}
