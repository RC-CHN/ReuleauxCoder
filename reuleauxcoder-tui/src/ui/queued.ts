import type {RuntimeState} from '@reuleauxcoder/client';
import {safe} from './format.js';
import {fit, paint, rail, section} from './theme.js';

export function queuedRows(state: RuntimeState, width: number, height: number): string[] {
  const prompts = state.queued_steering;
  const commands = state.queued_commands;
  const count = prompts.length + commands.length;
  if (!count || height < 1) return [];
  // Give both queues space; their internal order is preserved independently.
  const entries: {label: string; text: string}[] = [];
  for (let index = 0; index < Math.max(prompts.length, commands.length); index++) {
    for (const [label, items] of [['Prompt', prompts], ['Command', commands]] as const) {
      if (index < items.length) entries.push({label: `${label} ${index + 1}`, text: items[index]});
    }
  }
  if (height === 1) return [paint.panel(fit(paint.accent(`↳ QUEUED ${count} · `) + safe(prompts[0] ?? commands[0]).replace(/\s+/g, ' '), width))];
  const visible = Math.min(count, height - 1);
  const detail = visible < count ? `+${count - visible} more · F2 all` : 'F2 full text';
  const rows = entries.slice(0, visible).map(entry => rail(
    (entry.label.startsWith('Command') ? paint.info : paint.accent)(entry.label + ' · ') + paint.muted(safe(entry.text).replace(/\s+/g, ' ').trim()), width,
  ));
  return [section(`QUEUED ${count}`, detail, width), ...rows.map(row => paint.panel(row))];
}
