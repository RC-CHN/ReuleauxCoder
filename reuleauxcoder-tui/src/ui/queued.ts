import type {RuntimeState} from '@reuleauxcoder/client';
import {safe} from './format.js';
import {fit, paint, rail, section} from './theme.js';
import type {Cell} from '../state/session.js';

interface QueueItem {id: string; text: string; status: string}

export function queuedRows(state: RuntimeState, width: number, height: number, pending: Cell[] = [], controls?: {items: QueueItem[]; selected: string | null; enabled: boolean; busy: boolean}): string[] {
  const prompts = controls?.items ?? [
    ...pending.map(cell => ({id: cell.id, text: cell.body, status: cell.tone})),
    ...state.queued_steering.map(text => ({id: '', text, status: 'queued'})),
  ];
  const commands = state.queued_commands;
  const count = prompts.length + commands.length;
  if (!count || height < 1) return [];
  // Give both queues space; their internal order is preserved independently.
  const entries: {label: string; text: string; selected?: boolean}[] = [];
  for (let index = 0; index < Math.max(prompts.length, commands.length); index++) {
    if (index < prompts.length) {
      const prompt = prompts[index];
      const status = prompt.status === 'cancelled' ? 'withdrawn · Enter to edit' : prompt.status === 'not-applied' ? 'not applied · F3 to edit' : prompt.status === 'queued' ? '' : prompt.status;
      entries.push({label: `Prompt ${index + 1}${status ? ` · ${status}` : ''}`, text: prompt.text, selected: !!controls?.selected && prompt.id === controls.selected});
    }
    if (index < commands.length) entries.push({label: `Command ${index + 1}`, text: commands[index]});
  }
  const title = prompts.some(prompt => prompt.status !== 'queued') ? 'OUTBOX' : 'QUEUED';
  if (height === 1) return [paint.panel(fit(paint.accent(`↳ ${title} ${count} · `) + safe(prompts[0]?.text ?? commands[0]).replace(/\s+/g, ' '), width))];
  const visible = Math.min(count, height - 1);
  const detail = controls?.selected ? controls.busy ? 'Withdrawing…' : '↑↓ select · Enter edit · Del cancel · Esc back' : `${visible < count ? `+${count - visible} more · ` : ''}${controls?.enabled && prompts.length ? 'F3 manage' : 'F2 full text'}`;
  const selected = entries.findIndex(entry => entry.selected);
  const start = Math.max(0, selected - visible + 1);
  const rows = entries.slice(start, start + visible).map(entry => rail(
    (entry.label.startsWith('Command') ? paint.info : paint.accent)((entry.selected ? '› ' : '') + entry.label + ' · ') + paint.muted(safe(entry.text).replace(/\s+/g, ' ').trim()), width,
  ));
  return [section(`${title} ${count}`, detail, width), ...rows.map(row => paint.panel(row))];
}
