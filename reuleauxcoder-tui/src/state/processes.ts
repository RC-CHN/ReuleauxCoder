import type {RecordData} from '@reuleauxcoder/client';

export interface ProcessView extends RecordData {
  process_session_id: string;
  command: string;
  state: string;
  elapsed_seconds: number;
  receivedAt: number;
  outputTail: {stdout: string; stderr: string};
  background?: boolean;
  notified?: boolean;
}

/** Keep partial lines across events; an exit event without output must not erase the tail. */
export function updateProcess(previous: ProcessView | undefined, event: RecordData, now = Date.now()): ProcessView {
  const tail = (stream: 'stdout' | 'stderr') => ((previous?.outputTail[stream] ?? '') + (event[stream] ?? '')).slice(-2048).split('\n').slice(-4).join('\n');
  return {...previous, ...event, receivedAt: now, outputTail: {stdout: tail('stdout'), stderr: tail('stderr')}} as ProcessView;
}

export function processElapsed(process: ProcessView, now = Date.now()): number {
  if (process.state !== 'running') return process.elapsed_seconds;
  return Math.max(0, process.elapsed_seconds + (now - process.receivedAt) / 1000);
}

export function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  return total < 60 ? `${total}s` : total < 3600 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${Math.floor(total / 3600)}h ${Math.floor(total % 3600 / 60)}m`;
}
