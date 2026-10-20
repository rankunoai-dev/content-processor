import type { Block } from '../../api/types';
import type { ViewParams } from '../../state/viewParams';
import { AREA_GROUP_ORDER, VISIBLE_LAYERS, areaGroup, type AreaGroup } from '../../vocabulary';

/** Above this many rows the list is virtualised. */
export const VIRTUALIZE_AT = 300;

export type Item =
  | { kind: 'section'; key: string; group: AreaGroup; count: number }
  | { kind: 'heading'; key: string; text: string; level: number }
  | { kind: 'block'; key: string; block: Block };

export function selectBlocks(blocks: Block[], view: Pick<ViewParams, 'areas' | 'hidden' | 'unconfirmed' | 'find'>): Block[] {
  const needle = view.find.trim().toLocaleLowerCase();
  return blocks.filter(
    (b) =>
      view.areas.includes(areaGroup(b)) &&
      (view.hidden || VISIBLE_LAYERS.has(b.layer)) &&
      (!view.unconfirmed || !b.verified) &&
      (!needle || b.text.toLocaleLowerCase().includes(needle)),
  );
}

export function buildItems(blocks: Block[]): Item[] {
  const items: Item[] = [];
  for (const group of AREA_GROUP_ORDER) {
    const inGroup = blocks.filter((b) => areaGroup(b) === group).sort((a, b) => a.order - b.order);
    if (inGroup.length === 0) continue;
    items.push({ kind: 'section', key: `s-${group}`, group, count: inGroup.length });
    let last = '';
    for (const block of inGroup) {
      const path = block.heading_path;
      const joined = path.join('\u0000');
      if (path.length > 0 && joined !== last) {
        const title = path[path.length - 1] ?? '';
        if (title) {
          items.push({ kind: 'heading', key: `h-${block.id}`, text: title, level: Math.min(path.length, 4) });
        }
        last = joined;
        if (block.text.trim() === title.trim()) continue; // the heading's own text
      }
      items.push({ kind: 'block', key: block.id, block });
    }
  }
  return items;
}

