import {
  CheckCircleFilled,
  CopyOutlined,
  ExportOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons';
import { App, Button, Space, Tag, Tooltip, Typography } from 'antd';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { usePrefetchPage } from '../api/queries';
import { usePreferences } from '../state/preferences';
import { pageHref } from '../state/viewParams';
import { APP, badgeInfo, issueInfo } from '../vocabulary';

/** The engine name, shown only when "Show technical names" is on. */
export function TechName({ name }: { name: string | null | undefined }) {
  const show = usePreferences((s) => s.showTechNames);
  if (!show || !name) return null;
  return (
    <Typography.Text code className="tech-name">
      {name}
    </Typography.Text>
  );
}

export function StatusTag({ badge, withHelp = true }: { badge: string; withHelp?: boolean }) {
  const info = badgeInfo(badge);
  const tag = (
    <Tag color={info.tag} className="status-tag" bordered={false}>
      {info.label}
    </Tag>
  );
  return (
    <span className="nowrap">
      {withHelp ? <Tooltip title={info.meaning}>{tag}</Tooltip> : tag}
      <TechName name={badge} />
    </span>
  );
}

export function IssueTag({ issue }: { issue: string }) {
  const info = issueInfo(issue);
  return (
    <Tooltip title={info.why}>
      <Tag bordered={false} color={info.level === 'error' ? 'red' : info.level === 'warning' ? 'orange' : 'blue'}>
        {info.title}
      </Tag>
    </Tooltip>
  );
}

export function HelpTip({ text }: { text: ReactNode }) {
  return (
    <Tooltip title={text}>
      <QuestionCircleOutlined className="help-icon" aria-label="More information" />
    </Tooltip>
  );
}

export function PageHeading({
  title,
  subtitle,
  extra,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div className="page-heading-text">
        <Typography.Title level={2} className="page-title">
          {title}
        </Typography.Title>
        {subtitle ? <Typography.Paragraph type="secondary" className="page-subtitle">{subtitle}</Typography.Paragraph> : null}
      </div>
      {extra ? <div className="page-heading-extra">{extra}</div> : null}
    </div>
  );
}

/** Title link to a page of the run; warms its detail on hover. Falls back to plain text. */
export function PageLink({
  run,
  pageKey,
  title,
  url,
}: {
  run: string;
  pageKey: string | null;
  title: string;
  url: string;
}) {
  const prefetch = usePrefetchPage(run);
  const body = (
    <span className="page-link-body">
      <span className="page-link-title">{title}</span>
      <Typography.Text type="secondary" className="page-link-url" ellipsis={{ tooltip: url }}>
        {url}
      </Typography.Text>
    </span>
  );
  if (!pageKey) return body;
  return (
    <Link
      to={pageHref(run, pageKey)}
      className="page-link"
      onMouseEnter={() => prefetch(pageKey)}
      onFocus={() => prefetch(pageKey)}
    >
      {body}
    </Link>
  );
}

export function CopyButton({ text, label = APP.copyUrl }: { text: string; label?: string }) {
  const { message } = App.useApp();
  return (
    <Tooltip title={label}>
      <Button
        type="text"
        size="small"
        icon={<CopyOutlined />}
        aria-label={label}
        onClick={() => {
          navigator.clipboard
            .writeText(text)
            .then(() => message.success(APP.copied))
            .catch(() => message.error(APP.copyFailed));
        }}
      />
    </Tooltip>
  );
}

export function OpenButton({ url }: { url: string }) {
  return (
    <Tooltip title={APP.openOriginal}>
      <Button
        type="text"
        size="small"
        icon={<ExportOutlined />}
        href={url}
        target="_blank"
        rel="noopener noreferrer nofollow"
        aria-label={APP.openOriginal}
      />
    </Tooltip>
  );
}

/** A URL as text with copy and open-in-new-tab buttons. */
export function UrlLine({ url }: { url: string }) {
  return (
    <Space size={2} className="url-line">
      <Typography.Text className="url-text" ellipsis={{ tooltip: url }}>
        {url}
      </Typography.Text>
      <CopyButton text={url} />
      <OpenButton url={url} />
    </Space>
  );
}

export function VerifiedIcon({
  verified,
  yes,
  no,
  short,
}: {
  verified: boolean;
  yes: string;
  no: string;
  short: string;
}) {
  return verified ? (
    <Tooltip title={yes}>
      <CheckCircleFilled className="verified-icon" aria-label={yes} />
    </Tooltip>
  ) : (
    <Tooltip title={no}>
      <Tag bordered={false} color="blue" className="unconfirmed-tag">
        {short}
      </Tag>
    </Tooltip>
  );
}

export function ReportSourceNote({
  source,
  generatedAt,
  stale,
}: {
  source: 'report' | 'computed';
  generatedAt: string | null;
  stale: boolean;
}) {
  return (
    <Space direction="vertical" size={4}>
      <Typography.Text type="secondary" className="small-note">
        {source === 'report'
          ? `${APP.savedReport}${generatedAt ? ` (${new Date(generatedAt).toLocaleString()})` : ''}.`
          : `${APP.computedReport}.`}
      </Typography.Text>
      {stale ? (
        <Typography.Text type="warning" className="small-note">
          {APP.staleReport}
        </Typography.Text>
      ) : null}
    </Space>
  );
}
