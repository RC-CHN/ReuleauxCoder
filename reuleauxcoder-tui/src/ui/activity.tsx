import React from 'react';
import {Box, Text} from 'ink';
import type {TuiController} from '../state/controller.js';
import {safe} from './format.js';
import {between, fit, paint, rail} from './theme.js';
import {isProcessPoll} from './tool-groups.js';
import {useMotion} from './motion.js';

export function activityFor(c: TuiController) {
  if (c.session.fatal) return null;
  if (c.closing) return {label: safe(c.shutdownProgress), moving: true};
  if (!c.session.connected) return {label: 'Connecting…', moving: true};
  if (c.active || c.session.state.approval_waiting) return {label: 'Waiting for your input', moving: false};
  if (c.session.state.stopping) return {label: 'Stopping…', moving: true};
  if (!c.session.state.running) return null;
  const cell = c.session.activeCell;
  if (cell && isProcessPoll(cell)) {
    const id = cell.tool!.arguments.session_id;
    return {label: `Waiting · ${safe(c.session.processes.get(id)?.command || id)}`, moving: true};
  }
  const label = cell?.kind === 'tool' ? `Running ${safe(cell.title)}…`
    : cell?.kind === 'reasoning' ? 'Thinking…'
    : cell?.kind === 'assistant' ? 'Responding…' : 'Waiting for model…';
  return {label, moving: true};
}

const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

/** Keep animation ticks out of the transcript layout and controller state. */
export function ActivityLine({label, moving, width}: {label: string; moving: boolean; width: number}) {
  const time = useMotion(moving);
  const color = moving ? paint.secondary : paint.warning;
  // Keep the one-second spinner cycle while its brightness changes between glyphs.
  const marker = moving ? paint.borderGlow(frames[Math.floor(time / 100) % frames.length], 'secondary', 0.7 + 0.3 * Math.cos(time * Math.PI * 2 / 1000)) : color('◇');
  const text = `${marker} ${color(label)}`;
  const elapsed = moving ? paint.muted(`${Math.floor(time / 1000)}s`) : '';
  return <Box height={1} flexShrink={0}><Text wrap="truncate">{paint.surface(fit(rail(between(text, elapsed, width - 2), width, color), width))}</Text></Box>;
}
