import type {TuiController} from '../state/controller.js';
import {activityFor} from './activity.js';
import {safe} from './format.js';
import {fit, keyHint, paint, section} from './theme.js';
import {duration, processElapsed} from '../state/processes.js';

export function workbenchLayout(columns: number, rows: number) {
  const width = Math.max(8, columns - 2);
  const sidebar = columns >= 120 && rows >= 20 ? Math.min(36, Math.floor(columns / 4)) : 0;
  return {width, sidebar, main: width - (sidebar ? sidebar + 3 : 0)};
}

const compact = (text: string) => safe(text).replace(/\s+/g, ' ').trim();
const number = new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1});
const valueRow = (label: string, value: string, color = paint.info) => paint.muted(label + ' ') + color(compact(value));
interface Block {title: string; summary: string[]; details: string[]; color: (text: string) => string; detailPriority: number; caption?: string}

/** Allocate summaries by importance, then spend spare rows on details. F2 keeps all facts. */
export function sidebarRows(c: TuiController, width: number, height: number, now = Date.now()): string[] {
  const {state, plan, jobs, processes, git} = c.session;
  const blocks: Block[] = [];
  const add = (title: string, summary: string[], details: string[], color = paint.secondary, detailPriority = 9, caption = '') => {
    if (summary.length) blocks.push({title, summary, details, color, detailPriority, caption});
  };
  const conflicts = git?.files.filter(file => file.conflict) ?? [];
  const issues = [
    ...(c.session.fatal ? [paint.error(compact(c.session.fatal))] : []),
    ...(c.active ? [paint.warning(compact(c.active.request.title)), paint.warning(`${Math.max(c.client.interactions.length, state.approval_waiting + 1)} awaiting input`)] : state.approval_waiting ? [paint.warning(`${state.approval_waiting} awaiting input`)] : []),
    ...(conflicts.length ? [paint.error(`${conflicts.length} Git conflicts`)] : []),
    ...[...jobs.values()].filter(job => ['failed', 'blocked'].includes(job.status)).map(job => paint.warning(`${job.status}: ${compact(job.task)}`)),
  ];
  add('ATTENTION', issues.slice(0, 2), issues.slice(2), paint.warning, 0);

  const goal = state.goal;
  if (goal) {
    const color = goal.status === 'active' ? paint.accent : goal.status === 'complete' ? paint.success : paint.warning;
    const budget = goal.token_budget === null ? 'No limit' : number.format(goal.token_budget);
    const goalLabel = goal.status === 'paused' && state.running ? 'pausing' : goal.status.replaceAll('_', ' ');
    add('GOAL', [color(goalLabel), paint.info(compact(goal.objective))], [
      valueRow('Tokens', `${number.format(goal.tokens_used)} / ${budget}`),
      valueRow('Elapsed', duration(goal.time_used_seconds)),
      ...(goal.estimated_requests ? [paint.warning(`${goal.estimated_requests} requests estimated`)] : []),
      paint.muted('/goal · Manage'),
    ], color, 1);
  }

  const live = activityFor(c);
  if (live && !c.active) add('EXECUTION', [paint.secondary(live.label)], c.session.progress.summary ? [paint.info(compact(c.session.progress.summary))] : [], paint.secondary, 1);
  const items = plan.items ?? [];
  const activeIndex = items.findIndex((item: any) => item.status === 'in_progress');
  const focus = activeIndex >= 0 ? activeIndex : items.findIndex((item: any) => item.status !== 'completed');
  const planRow = (item: any, index: number) => item.status === 'in_progress'
    ? paint.badge(fit(`[${String(index + 1).padStart(2, '0')}] ${compact(item.step)}`, width - 2))
    : (item.status === 'completed' ? paint.success : paint.muted)(`${item.status === 'completed' ? '[✓]' : '[ ]'} ${compact(item.step)}`);
  if (items.length) add('PLAN', [focus >= 0 ? planRow(items[focus], focus) : paint.success('✓ Plan completed')], items.map(planRow).filter((_: string, index: number) => index !== focus), paint.accent, 7, `${items.filter((item: any) => item.status === 'completed').length}/${items.length}`);

  const liveProcesses = [...processes.values()].filter(process => process.state !== 'exited');
  if (liveProcesses.length) {
    const visibleProcesses = liveProcesses.slice(0, 3);
    const rows = visibleProcesses.flatMap(process => {
      const color = process.state === 'unknown' ? paint.warning : paint.secondary;
      const elapsed = duration(processElapsed(process, now));
      const output = [
        ...process.outputTail.stdout.split('\n').filter(line => line.trim()).slice(-2).map(line => paint.muted('  ▏ ') + paint.info(compact(line))),
        ...process.outputTail.stderr.split('\n').filter(line => line.trim()).slice(-1).map(line => paint.muted('  ▏ ') + paint.warning(compact(line))),
      ];
      return [color(`● ${process.state} · ${elapsed}${process.runtime_timeout_seconds ? ' / ' + duration(process.runtime_timeout_seconds) : ''}`), paint.info(compact(process.command)), ...output, ...(process.output_truncated ? [paint.warning('  Output truncated · /ps')] : [])];
    });
    if (liveProcesses.length > visibleProcesses.length) rows.push(paint.muted(`and ${liveProcesses.length - visibleProcesses.length} more · /ps`));
    add('PROCESSES', [paint.secondary(`${liveProcesses.length} active · /ps manage`), ...rows.slice(0, 2)], rows.slice(2), paint.secondary, 2);
  }

  if (git?.available) {
    const count = `${git.truncated ? '≥' : ''}${git.files.length}`;
    const summary = [git.files.length || git.truncated ? paint.accent(`${count} changed`) + (git.additions === null ? '' : ` · ${paint.success('+' + git.additions)} ${paint.error('−' + git.deletions)}`) : paint.success('✓ Working tree clean')];
    if (git.upstream && git.ahead !== null && git.behind !== null) summary.push(paint.info(`↑ ${git.ahead} ahead · ↓ ${git.behind} behind`));
    const staged = git.files.filter(file => !file.conflict && !['.', '?'].includes(file.index)).length;
    const unstaged = git.files.filter(file => !file.conflict && !['.', '?'].includes(file.worktree)).length;
    const untracked = git.files.filter(file => file.index === '?').length;
    const visibleFiles = git.files.slice(0, 4);
    const details = [paint.muted(`${staged} staged · ${unstaged} unstaged`), paint.info(`${untracked} untracked`), ...visibleFiles.map(file => {
      const status = file.index + file.worktree;
      const color = file.conflict ? paint.error : status.includes('D') ? paint.error : status.includes('A') ? paint.success : status.includes('?') ? paint.info : paint.accent;
      return color(status) + ' ' + paint.info(compact(file.path));
    }), ...(git.files.length > visibleFiles.length ? [paint.muted(`and ${git.truncated ? '≥' : ''}${git.files.length - visibleFiles.length} more · F2`)] : []), paint.muted(git.upstream ? `Upstream: ${compact(git.upstream)} (local)` : 'No upstream configured')];
    if (git.truncated) details.unshift(paint.warning('Partial Git scan · totals incomplete'));
    add('GIT', summary, details, git.branch === '(detached)' ? paint.warning : paint.info, 6, git.branch === '(detached)' ? `${git.head.slice(0, 8)} detached` : compact(git.branch));
  } else if (git) add('GIT', [paint.muted(git.reason || 'Unavailable')], [], paint.info);

  const ratio = state.context_limit ? Math.min(1, Math.max(0, state.context_tokens / state.context_limit)) : 0;
  const contextColor = ratio >= .9 ? paint.error : ratio >= .75 ? paint.warning : paint.secondary;
  const summary = [valueRow('Context', state.context_limit ? `${Math.round(state.context_tokens / state.context_limit * 100)}%` : '—', contextColor), valueRow('Model', state.model || '—')];
  if (state.mode) summary.push(valueRow('Mode', state.mode, paint.accent));
  if (state.approval_policy) summary.push(valueRow('Approval default', ({require_approval: 'Ask', allow: 'Allow', warn: 'Warn', deny: 'Deny'} as Record<string, string>)[state.approval_policy] ?? state.approval_policy, paint.warning));
  const filled = Math.round(ratio * 12);
  add('SESSION', summary, [
    ...(state.context_limit ? [contextColor('█'.repeat(filled)) + paint.border('░'.repeat(12 - filled))] : []),
    valueRow('Context tokens', number.format(state.context_tokens)),
    ...(state.mcp_tools ? [valueRow('MCP tools', String(state.mcp_tools))] : []),
  ], paint.secondary, 8);

  const activities = [
    ...[...jobs.values()].filter(job => !['cancelled', 'stale'].includes(job.status) && job.activity !== 'injected; next parent request').map(job => ({title: job.task, status: job.status, detail: job.blocker || job.error || job.current_tool || job.activity || ''})),
  ];
  add('ACTIVITY', activities.length ? [paint.secondary(`${activities.length} agents`)] : [], activities.flatMap(item => [paint.secondary('● ') + paint.info(compact(item.title)) + paint.muted(` · ${item.status}`), ...(item.detail ? [paint.muted('  ' + compact(item.detail))] : [])]), paint.secondary, 5);

  let spare = Math.max(0, height - 2);
  const visible: {block: Block; details: string[]}[] = [];
  for (const block of blocks) {
    const count = Math.min(block.summary.length, spare - 1);
    if (count > 0) {
      visible.push({block: {...block, summary: block.summary.slice(0, count), details: [...block.summary.slice(count), ...block.details]}, details: []});
      spare -= count + 1;
    }
  }
  for (const group of [...visible].sort((a, b) => a.block.detailPriority - b.block.detailPriority)) {
    const count = Math.min(spare, group.block.details.length);
    group.details = group.block.details.slice(0, count);
    if (count && count < group.block.details.length) group.details[count - 1] = paint.muted('More details · F2 all');
    spare -= count;
  }
  const rows = [section('WORKBENCH', '', width, paint.secondary)];
  for (const {block, details} of visible) {
    rows.push(section(block.title, block.caption || '', width, block.color), ...block.summary, ...details);
    if (spare > 0) {rows.push(''); spare--;}
  }
  while (rows.length < height - 1) rows.push('');
  return [...rows, keyHint('F2', 'Full session details')].map(row => fit(row, width));
}
