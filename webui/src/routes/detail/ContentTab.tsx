import { CopyOutlined } from '@ant-design/icons';
import { App, Button, Empty, Input, Space, Switch, Tag, Typography } from 'antd';
import VirtualList from 'rc-virtual-list';
import { Fragment, useMemo, type ReactNode } from 'react';
import type { Block, PageDetail } from '../../api/types';
import { VIRTUALIZE_AT, buildItems, selectBlocks, type Item } from './contentModel';
import { HelpTip, TechName, VerifiedIcon } from '../../components/bits';
import type { ViewParams } from '../../state/viewParams';
import {
  APP,
  AREA_GROUPS,
  AREA_GROUP_ORDER,
  DETAIL,
  LAYERS,
  VISIBLE_LAYERS,
  areaGroup,
  formatLabel,
  regionLabel,
} from '../../vocabulary';

const VIRTUAL_HEIGHT = 720;

/** Page text is untrusted: matches are highlighted by splitting the string, never as markup. */
export function Highlight({ text, needle }: { text: string; needle: string }) {
  if (!needle) return <>{text}</>;
  const lower = text.toLocaleLowerCase();
  const n = needle.toLocaleLowerCase();
  const parts: ReactNode[] = [];
  let i = 0;
  let at = lower.indexOf(n);
  while (at !== -1 && n) {
    if (at > i) parts.push(text.slice(i, at));
    parts.push(<mark key={at}>{text.slice(at, at + n.length)}</mark>);
    i = at + n.length;
    at = lower.indexOf(n, i);
  }
  parts.push(text.slice(i));
  return (
    <>
      {parts.map((p, k) => (
        <Fragment key={k}>{p}</Fragment>
      ))}
    </>
  );
}

function BlockRow({ block, needle }: { block: Block; needle: string }) {
  const hiddenLayer = !VISIBLE_LAYERS.has(block.layer);
  return (
    <div className={`content-block${hiddenLayer ? ' content-block-hidden' : ''}`}>
      <Typography.Paragraph className="content-text">
        <Highlight text={block.text} needle={needle} />
      </Typography.Paragraph>
      <Space size={4} wrap className="content-meta">
        {block.region && block.region !== 'main_content' ? (
          <Tag bordered={false} className="meta-tag">
            {regionLabel(block.region)}
          </Tag>
        ) : null}
        {block.layer !== 'rendered_visible' ? (
          <Tag bordered={false} color={hiddenLayer ? 'purple' : 'geekblue'} className="meta-tag">
            {LAYERS[block.layer] ?? block.layer}
          </Tag>
        ) : null}
        {block.format && block.format !== 'html' ? (
          <Tag bordered={false} className="meta-tag">
            {formatLabel(block.format)}
            {block.page_label ? ` · p. ${block.page_label}` : ''}
          </Tag>
        ) : null}
        <TechName name={`${block.layer} · ${block.region ?? block.role} · ${block.status}`} />
        <VerifiedIcon verified={block.verified} yes={DETAIL.verified} no={DETAIL.notVerified} short={DETAIL.notVerifiedShort} />
      </Space>
    </div>
  );
}

function ItemRow({ item, needle }: { item: Item; needle: string }) {
  if (item.kind === 'section') {
    return (
      <div className="content-section-title">
        <Typography.Title level={5}>{AREA_GROUPS[item.group].label}</Typography.Title>
        <Typography.Text type="secondary">{item.count.toLocaleString()}</Typography.Text>
      </div>
    );
  }
  if (item.kind === 'heading') {
    return (
      <div className={`content-heading content-heading-${item.level}`}>
        <Highlight text={item.text} needle={needle} />
      </div>
    );
  }
  return <BlockRow block={item.block} needle={needle} />;
}

export function ContentTab({
  detail,
  view,
  setView,
}: {
  detail: PageDetail;
  view: ViewParams;
  setView: (patch: Partial<ViewParams>, options?: { replace?: boolean }) => void;
}) {
  const { message } = App.useApp();
  const shownBlocks = useMemo(() => selectBlocks(detail.blocks, view), [detail.blocks, view]);
  const items = useMemo(() => buildItems(shownBlocks), [shownBlocks]);
  const groupCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const b of selectBlocks(detail.blocks, { ...view, areas: AREA_GROUP_ORDER })) {
      const g = areaGroup(b);
      counts[g] = (counts[g] ?? 0) + 1;
    }
    return counts;
  }, [detail.blocks, view]);
  const hasUnconfirmed = detail.blocks.some((b) => !b.verified);
  const needle = view.find.trim();

  const copy = () => {
    const text = items
      .filter((i) => i.kind !== 'section')
      .map((i) => (i.kind === 'heading' ? i.text : i.kind === 'block' ? i.block.text : ''))
      .join('\n');
    navigator.clipboard
      .writeText(text)
      .then(() => message.success(DETAIL.copySuccess))
      .catch(() => message.error(APP.copyFailed));
  };

  if (detail.blocks.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="empty-text">{DETAIL.noBlocks}</span>} />;
  }

  return (
    <div className="content-tab">
      <div className="content-toolbar">
        <Space size={[8, 8]} wrap>
          <Typography.Text strong>{DETAIL.show}</Typography.Text>
          {AREA_GROUP_ORDER.map((group) => {
            const checked = view.areas.includes(group);
            const count = groupCounts[group] ?? 0;
            if (count === 0 && !checked) return null;
            return (
              <Tag.CheckableTag
                key={group}
                checked={checked}
                className="area-toggle"
                aria-pressed={checked}
                onChange={(on) =>
                  setView(
                    { areas: on ? [...view.areas, group] : view.areas.filter((a) => a !== group) },
                    { replace: true },
                  )
                }
              >
                <span title={AREA_GROUPS[group].help}>
                  {AREA_GROUPS[group].label} <span className="area-count">{count}</span>
                </span>
              </Tag.CheckableTag>
            );
          })}
        </Space>
        <div className="content-toolbar-row">
          <Input.Search
            allowClear
            aria-label={DETAIL.searchPage}
            placeholder={DETAIL.searchPage}
            value={view.find}
            onChange={(e) => setView({ find: e.target.value }, { replace: true })}
            className="search-box"
          />
          <Space size={8} className="nowrap">
            <Switch id="hidden-text" checked={view.hidden} onChange={(on) => setView({ hidden: on }, { replace: true })} />
            <label htmlFor="hidden-text">{DETAIL.includeHidden}</label>
            <HelpTip text={DETAIL.includeHiddenHelp} />
          </Space>
          {hasUnconfirmed ? (
            <Space size={8} className="nowrap">
              <Switch id="unconfirmed" checked={view.unconfirmed} onChange={(on) => setView({ unconfirmed: on }, { replace: true })} />
              <label htmlFor="unconfirmed">{DETAIL.onlyUnconfirmed}</label>
            </Space>
          ) : null}
          <span className="toolbar-spacer" />
          <Button icon={<CopyOutlined />} onClick={copy} disabled={shownBlocks.length === 0}>
            {DETAIL.copyVisible}
          </Button>
        </div>
        <Typography.Text type="secondary" className="small-note">
          {DETAIL.shownCount(shownBlocks.length, detail.blocks.length)}
          {detail.repeated_blocks > 0 ? ` · ${DETAIL.repeated(detail.repeated_blocks)}` : ''}
        </Typography.Text>
      </div>
      {items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="empty-text">{DETAIL.noBlocksFiltered}</span>} />
      ) : items.length > VIRTUALIZE_AT ? (
        <div className="content-body content-body-virtual" data-testid="virtual-content">
          <VirtualList data={items} height={VIRTUAL_HEIGHT} itemHeight={56} itemKey="key">
            {(item: Item) => <ItemRow item={item} needle={needle} />}
          </VirtualList>
        </div>
      ) : (
        <div className="content-body">
          {items.map((item) => (
            <ItemRow key={item.key} item={item} needle={needle} />
          ))}
        </div>
      )}
    </div>
  );
}
