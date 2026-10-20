import { Descriptions, List, Space, Tag, Typography } from 'antd';
import { Link } from 'react-router-dom';
import type { PageDetail } from '../../api/types';
import { TechName, UrlLine } from '../../components/bits';
import { pageHref } from '../../state/viewParams';
import {
  CHANGES,
  DETAIL,
  LINK_KINDS,
  changeLabel,
  dateSourceLabel,
  describeWarning,
  formatDate,
  formatLabel,
  humanize,
  tierLabel,
} from '../../vocabulary';

const F = DETAIL.facts;

function Missing() {
  return <Typography.Text type="secondary">{F.notRecorded}</Typography.Text>;
}

export function FactsTab({ detail, run }: { detail: PageDetail; run: string }) {
  const row = detail.row;
  const modified = formatDate(row.page_modified_at, true);
  const source = dateSourceLabel(detail.page_modified_source);
  const finalDiffers = row.final_url && row.final_url !== detail.requested_url;
  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <Descriptions bordered column={1} size="small" className="facts">
        <Descriptions.Item label={F.requested}>
          <UrlLine url={detail.requested_url} />
        </Descriptions.Item>
        <Descriptions.Item label={F.final}>
          {row.final_url ? (finalDiffers ? <UrlLine url={row.final_url} /> : F.sameAddress) : <Missing />}
        </Descriptions.Item>
        {detail.aliases.length > 0 ? (
          <Descriptions.Item label={F.aliases}>
            <Space direction="vertical" size={2}>
              {detail.aliases.map((a) => (
                <UrlLine key={a} url={a} />
              ))}
            </Space>
          </Descriptions.Item>
        ) : null}
        <Descriptions.Item label={F.captured}>{formatDate(row.captured_at, true)}</Descriptions.Item>
        <Descriptions.Item label={F.modified}>
          {modified ? (
            <>
              {modified}{' '}
              {source ? <Typography.Text type="secondary">({F.modifiedSource(source)})</Typography.Text> : null}
              <TechName name={detail.page_modified_source} />
            </>
          ) : (
            <Missing />
          )}
        </Descriptions.Item>
        <Descriptions.Item label={F.published}>
          {detail.page_published_at ? formatDate(detail.page_published_at) : <Missing />}
        </Descriptions.Item>
        <Descriptions.Item label={F.change}>
          {row.change_status ? (
            <Space direction="vertical" size={2}>
              <Tag bordered={false} color={CHANGES[row.change_status]?.tag}>
                {changeLabel(row.change_status)}
              </Tag>
              {detail.change_basis ? (
                <Typography.Text type="secondary">
                  {F.changeWhy}: {humanize(detail.change_basis)}
                </Typography.Text>
              ) : null}
              {detail.previous_run_id ? (
                <Typography.Text type="secondary">
                  {F.previousRun}: {detail.previous_run_id}
                </Typography.Text>
              ) : null}
              {detail.reused_from_run_id ? (
                <Typography.Text type="secondary">
                  {F.reused}: {detail.reused_from_run_id}
                </Typography.Text>
              ) : null}
            </Space>
          ) : (
            changeLabel(null)
          )}
        </Descriptions.Item>
        <Descriptions.Item label={F.access}>
          {tierLabel(detail.tier_reached)} <TechName name={detail.tier_reached} />
        </Descriptions.Item>
        <Descriptions.Item label={F.fileType}>
          {formatLabel(row.content_type)}{' '}
          {detail.sniffed_mime ? <Typography.Text type="secondary">({detail.sniffed_mime})</Typography.Text> : null}
        </Descriptions.Item>
        {detail.warnings.length > 0 ? (
          <Descriptions.Item label={F.warnings}>
            <ul className="plain-list">
              {[...new Set(detail.warnings)].map((w) => (
                <li key={w}>
                  {describeWarning(w)} <TechName name={w} />
                </li>
              ))}
            </ul>
          </Descriptions.Item>
        ) : null}
      </Descriptions>
      <div>
        <Typography.Title level={5}>{F.links}</Typography.Title>
        {detail.followups.length === 0 ? (
          <Typography.Text type="secondary">{F.linksNone}</Typography.Text>
        ) : (
          <List
            size="small"
            bordered
            dataSource={detail.followups}
            renderItem={(f) => (
              <List.Item
                key={f.url}
                extra={
                  f.page_key ? (
                    <Link to={pageHref(run, f.page_key)}>{F.inRun}</Link>
                  ) : null
                }
              >
                <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
                  <Space size={6}>
                    <Tag bordered={false}>{LINK_KINDS[f.edge_kind ?? ''] ?? humanize(f.edge_kind ?? 'link')}</Tag>
                    {f.expected_mime ? <Typography.Text type="secondary">{f.expected_mime}</Typography.Text> : null}
                  </Space>
                  <UrlLine url={f.url} />
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>
    </Space>
  );
}
