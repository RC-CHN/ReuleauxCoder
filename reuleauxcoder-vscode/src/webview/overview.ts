import type {HostSnapshot, WebRequest} from '../shared.js';
import {t, errorText, toolLabel, compactNumber as compact} from '../i18n.js';
import {coreMessage} from '../core-messages.js';
import {icon, type IconName} from './icons.js';
import type {LiveClock} from './clock.js';

export class WorkOverviewView {
  private signature = '';
  private open = false;
  constructor(private root: HTMLElement, private goal: HTMLElement, private toggle: HTMLButtonElement, private request: (action: string, data?: WebRequest['data']) => Promise<any>, private notice: (error: unknown) => void, private clock: LiveClock) {
    toggle.addEventListener('click', () => {this.open = !this.open; root.hidden = !this.open; toggle.setAttribute('aria-expanded', String(this.open));});
    root.addEventListener('keydown', event => {if (event.key === 'Escape') {this.open = false; root.hidden = true; toggle.setAttribute('aria-expanded', 'false'); toggle.focus();}});
  }
  update(state: HostSnapshot): void {
    const data = state.overview;
    const signature = JSON.stringify([data ? {...data, goalObservedAt: 0, goal: data.goal ? {...data.goal, time_used_seconds: 0} : null, processes: data.processes.map(process => ({...process, elapsed: 0, observedAt: 0}))} : null, state.phase, state.running, state.reviews.length, state.interactions?.length]);
    if (this.signature === signature) return; this.signature = signature;
    this.toggle.disabled = !data;
    if (!data) {this.root.hidden = true; this.goal.hidden = true; this.toggle.setAttribute('aria-expanded', 'false'); return;}
    this.root.hidden = !this.open; this.toggle.setAttribute('aria-expanded', String(this.open));
    const scroll = this.root.scrollTop;
    const focusKey = (document.activeElement as HTMLElement)?.dataset.overviewAction;
    const summary = document.createElement('div'); summary.className = 'overview-title'; summary.textContent = t('Work overview');
    const live = document.createElement('span'); live.className = 'overview-live'; live.textContent = state.phase === 'ready' ? (['Reasoning', 'Writing'].includes(data.activity) ? errorText(data.activity) : toolLabel(data.activity)) || (state.running ? t('Running') : t('Ready')) : ({idle: t('Idle'), starting: t('Connecting…'), failed: t('Failed'), installing: t('Installing…'), stopping: t('Saving and stopping…')})[state.phase]; summary.append(live);
    const stats = document.createElement('div'); stats.className = 'overview-stats';
    const ratio = data.contextLimit ? Math.round(data.contextTokens / data.contextLimit * 100) : null;
    stats.append(this.action(`${t('Context')} ${ratio === null ? '—' : `${ratio}%`}`, 'system.tokens', 'model'));
    const policy = ({require_approval: t('Ask'), allow: t('Allow'), warn: t('Warn'), deny: t('Deny')} as Record<string, string>)[data.approvalPolicy] ?? data.approvalPolicy;
    stats.append(this.action(`${t('Permissions')} · ${policy || '—'}`, 'approval.show', 'shield'));
    if (data.git?.available) stats.append(this.action(`${data.git.branch} · ${t(data.git.files.length === 1 && !data.git.truncated ? '{0} changed file' : '{0} changed files', `${data.git.files.length}${data.git.truncated ? '+' : ''}`)}`, undefined, 'history', () => void this.request('git').catch(this.notice)));
    const processes = data.processes.filter(item => item.state !== 'exited');
    const jobs = data.jobs.filter(item => !['completed', 'cancelled', 'stale'].includes(item.status));
    if (processes.length) stats.append(this.action(`${t('Processes')} ${processes.length}`, 'processes.list', 'terminal'));
    if (jobs.length) stats.append(this.action(`${t('Agents')} ${jobs.length}`, 'subagent.jobs.list', 'agents'));
    if (data.queued) stats.append(this.text(`${t('Queue')} · ${data.queued}`, 'stat-warning'));
    const conflicts = data.git?.files.filter(file => file.conflict).length ?? 0;
    if (conflicts) stats.append(this.text(t(conflicts === 1 ? '{0} conflict' : '{0} conflicts', conflicts), 'stat-warning'));
    const waiting = state.reviews.length + (state.interactions?.length ?? 0);
    if (waiting) stats.append(this.action(t('Waiting for you · {0}', waiting), undefined, 'shield', () => document.getElementById('reviews')!.scrollIntoView({block: 'start'})));
    const content = document.createElement('div'); content.className = 'overview-content';
    if (data.goal) this.section(content, `${t('Goal')} · ${errorText(data.goal.status)}`, [data.goal.objective], 'goal');
    if (data.progress) this.section(content, t('Progress'), [data.progress], 'progress');
    if (data.plan.length) {
      const completed = data.plan.filter(item => item.status === 'completed').length;
      const section = this.section(content, `${t('Plan')} · ${completed}/${data.plan.length}`, []);
      for (const [index, item] of data.plan.entries()) {const row = this.text(`${item.status === 'completed' ? '✓' : String(index + 1).padStart(2, '0')}  ${item.step}`, `plan-step ${item.status}`); row.title = errorText(item.status); section.append(row);}
    }
    if (processes.length) {
      const section = this.section(content, t('Processes'), []);
      for (const process of processes) {
        const row = this.action('', 'processes.list', 'terminal'); row.dataset.overviewAction = `process:${process.id}`;
        const label = row.querySelector('span')!;
        label.append(document.createTextNode(`${errorText(process.state)} · `), this.clock.element(`process:${process.id}`, process.elapsed), document.createTextNode(` · ${process.command}`));
        section.append(row); if (process.output) section.append(this.text(process.output.split('\n').slice(-3).join('\n'), 'process-tail'));
      }
    }
    if (jobs.length) {const section = this.section(content, t('Agents'), []); for (const job of jobs) {section.append(this.action(`${errorText(job.status)} · ${job.task}`, 'subagent.jobs.list', 'agents')); if (job.detail) section.append(this.text(job.detail, 'overview-detail'));}}
    if (data.git?.available) {
      const git = data.git; const section = this.section(content, t('Git changes'), [], 'git');
      if (git.files.length) {
        const counts = document.createElement('div'); counts.className = 'git-counts';
        const additions = document.createElement('span'); additions.className = 'git-added'; additions.textContent = git.additions === null ? '—' : '+' + git.additions;
        const deletions = document.createElement('span'); deletions.className = 'git-deleted'; deletions.textContent = git.deletions === null ? '—' : '−' + git.deletions;
        counts.append(additions, document.createTextNode(' / '), deletions); if (git.truncated) counts.append(' · …'); section.append(counts);
      } else section.append(this.text(t('Working tree clean'), 'git-added'));
      if (git.upstream) {const upstream = this.text(git.upstream, 'git-upstream'); const ahead = document.createElement('span'); ahead.textContent = ` ↑${git.ahead ?? '—'} ↓${git.behind ?? '—'}`; ahead.className = 'git-sync'; upstream.append(ahead); section.append(upstream);}
      for (const file of git.files.slice(0, 6)) {
        const row = this.action('', undefined, undefined, () => void this.request('openFile', {path: file.path}).catch(this.notice)); row.className = 'git-file'; row.title = file.path;
        const status = document.createElement('span'); status.textContent = `${file.index}${file.worktree}`; status.className = `git-status ${file.conflict ? 'conflict' : /D/.test(status.textContent) ? 'deleted' : /[A?]/.test(status.textContent) ? 'added' : 'modified'}`;
        const path = document.createElement('span'); path.textContent = file.path; row.replaceChildren(status, path); section.append(row);
      }
      section.append(this.action(t('View details'), undefined, 'expand', () => void this.request('git').catch(this.notice)));
    } else if (data.git?.reason) this.section(content, 'Git', [['git_timed_out', 'git_not_installed', 'git_unavailable', 'not_initialized', 'status_timed_out', 'status_failed'].includes(data.git.reason) ? errorText(data.git.reason) : coreMessage(data.git.reason)]);
    if (data.diagnostics.length) {
      const section = this.section(content, t('Diagnostics'), [], 'diagnostics');
      for (const item of data.diagnostics) {const row = this.action('', undefined, 'document', () => void this.request('openFile', {path: item.path}).catch(this.notice)); row.className = 'diagnostic-row'; const label = row.querySelector('span')!; const count = document.createElement('strong'); count.className = item.errors ? 'diagnostic-errors' : 'diagnostic-warnings'; count.textContent = t('{0} errors · {1} warnings', item.errors, item.warnings); label.append(count, document.createTextNode(' · ' + item.path)); section.append(row);}
    }
    this.section(content, t('Context'), [t('{0} tokens', `${compact(data.contextTokens)} / ${data.contextLimit ? compact(data.contextLimit) : '—'}`), `MCP · ${t(data.mcpTools === 1 ? '{0} tool' : '{0} tools', data.mcpTools)}`]);
    if (data.warnings.length) this.section(content, t('Attention'), data.warnings.map(coreMessage), 'warning');
    this.root.replaceChildren(summary, stats, content);
    this.goal.replaceChildren(); this.goal.hidden = !data.goal || data.goal.status === 'complete';
    if (data.goal) {
      const goal = data.goal; const title = this.action(goal.objective, 'goal.show', 'goal'); title.className = 'goal-objective'; title.title = goal.objective;
      const meta = this.text(`${errorText(goal.status)} · ${compact(goal.tokens_used)} / ${goal.token_budget === null ? t('No limit') : compact(goal.token_budget)} · `, 'goal-meta');
      meta.append(this.clock.element('goal', goal.time_used_seconds));
      this.goal.append(title, meta);
      if (goal.status !== 'complete') this.goal.append(this.action(goal.status === 'active' ? t('Pause') : t('Resume'), goal.status === 'active' ? 'goal.pause' : 'goal.resume'));
    }
    this.root.scrollTop = scroll;
    if (focusKey) [...this.root.querySelectorAll<HTMLElement>('[data-overview-action]'), ...this.goal.querySelectorAll<HTMLElement>('[data-overview-action]')].find(node => node.dataset.overviewAction === focusKey)?.focus({preventScroll: true});
  }
  private text(text: string, className = ''): HTMLElement {const node = document.createElement('div'); node.className = className; node.textContent = text; return node;}
  private action(text: string, actionId?: string, name?: IconName, click?: () => void): HTMLButtonElement {
    const button = document.createElement('button'); button.type = 'button'; button.dataset.overviewAction = actionId ?? text; if (name) button.append(icon(name)); const label = document.createElement('span'); label.textContent = text; button.append(label); button.addEventListener('click', click ?? (() => void this.request('command.open', {actionId}).catch(this.notice))); return button;
  }
  private section(root: HTMLElement, title: string, lines: string[], kind = 'default'): HTMLElement {
    const section = document.createElement('section'); section.dataset.kind = kind;
    const heading = document.createElement('h3'); const [label, ...detail] = title.split(' · '); heading.textContent = label;
    if (detail.length) {const badge = document.createElement('span'); badge.className = 'overview-badge'; badge.textContent = detail.join(' · '); heading.append(badge);}
    section.append(heading); for (const line of lines) section.append(this.text(line, 'overview-detail')); root.append(section); return section;
  }
}
