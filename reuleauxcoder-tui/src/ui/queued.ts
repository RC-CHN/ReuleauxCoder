import type {RuntimeState} from '@reuleauxcoder/client';
import {safe} from './format.js';
import {fit, paint, rail, section} from './theme.js';
import type {Cell} from '../state/session.js';

export function queuedRows(state: RuntimeState, width: number, height: number, pending: Cell[] = []): string[] {
  const prompts = [
    ...pending.map(cell => ({text: cell.body, status: cell.tone})),
    ...state.queued_steering.map(text => ({text, status: 'queued'})),
  ];
  const commands = state.queued_commands;
  const count = prompts.length + commands.length;
  if (!count || height < 1) return [];
  // Give both queues space; their internal order is preserved independently.
  const entries: {label: string; text: string}[] = [];
  for (let index = 0; index < Math.max(prompts.length, commands.length); index++) {
    if (index < prompts.length) {
      const prompt = prompts[index];
      const status = prompt.status === 'not-applied' ? 'not applied · Alt+Up to edit' : prompt.status === 'queued' ? '' : prompt.status;
      entries.push({label: `Prompt ${index + 1}${status ? ` · ${status}` : ''}`, text: prompt.text});
    }
    if (index < commands.length) entries.push({label: `Command ${index + 1}`, text: commands[index]});
  }
  const title = prompts.some(prompt => prompt.status !== 'queued') ? 'OUTBOX' : 'QUEUED';
  if (height === 1) return [paint.panel(fit(paint.accent(`↳ ${title} ${count} · `) + safe(prompts[0]?.text ?? commands[0]).replace(/\s+/g, ' '), width))];
  const visible = Math.min(count, height - 1);
  const detail = visible < count ? `+${count - visible} more · F2 all` : 'F2 full text';
  const rows = entries.slice(0, visible).map(entry => rail(
    (entry.label.startsWith('Command') ? paint.info : paint.accent)(entry.label + ' · ') + paint.muted(safe(entry.text).replace(/\s+/g, ' ').trim()), width,
  ));
  return [section(`${title} ${count}`, detail, width), ...rows.map(row => paint.panel(row))];
}
