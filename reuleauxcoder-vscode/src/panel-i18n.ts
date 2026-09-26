import type {Action, Panel} from '@reuleauxcoder/client';
import {coreText, errorText, isChinese, t, toolLabel} from './i18n.js';
import {templateText} from './core-messages.js';
import {skillText, skillSource} from './skill-text.js';

const processPanel = (panel: Panel) => panel.view_type.startsWith('process_session:');
const processList = (panel: Panel) => ['process_sessions', 'process_sessions_ended'].includes(panel.view_type);
const prefixLabel = (value: string) => {const [label, ...rest] = value.split(' · '); return [errorText(label), ...rest].join(' · ');};

export function panelTitle(panel: Panel): string {
  if (!isChinese()) return panel.title;
  if (processPanel(panel)) return prefixLabel(panel.title); // command after the status stays literal
  if (panel.view_type === 'agent_job_actions') return prefixLabel(panel.title);
  if (panel.view_type === 'model_profiles' && panel.title.startsWith('Model Profiles · ')) {
    return `${t('Model Profiles')} · ${errorText(panel.title.slice('Model Profiles · '.length))}`;
  }
  if (['approval_lifetime', 'approval_actions'].includes(panel.view_type)) {
    const parts = panel.title.split(' · '); const last = parts.length - 1;
    return parts.map((part, index) => index === 0 || panel.view_type === 'approval_actions' && index === last ? errorText(part) : part).join(' · ');
  }
  return templateText(panel.title, 'Compact context · estimated {0} / {1} tokens') ?? templateText(panel.title, 'Sub-agent Job {0}') ?? errorText(panel.title);
}

export function panelItemText(panel: Panel, item: Panel['items'][number]): {label: string; description: string} {
  const {label, description} = item;
  if (panel.view_type === 'skills' && item.details) {
    const text = skillText(item);
    return {label: text.label, description: [t(item.current ? 'Enabled' : 'Disabled'), skillSource(item.details.source), text.description].join(' · ')};
  }
  if (!isChinese()) return {label, description};
  const kind = panel.view_type;
  // Names, goals, previews and commands are content, even if they equal a UI key.
  if (kind === 'model_profiles') return {label, description: description.replace(/ · ctx (\d+)$/, (_, count) => ` · ${t('Context: {0}', count)}`)};
  if (kind === 'model_slots') return {label: errorText(label), description: description === '(none)' ? t('(none)') : description};
  if (['mode_profiles', 'modes'].includes(kind)) return {label: modeLabel(label), description};
  if (['skills', 'mcp_servers', 'mcp'].includes(kind) && item.action) {
    const parts = description.split(' · ');
    if (kind === 'skills') return {label, description: [errorText(parts[0]), parts[1] === 'builtin' ? t('Bundled skill') : errorText(parts[1] ?? ''), ...parts.slice(2)].filter(Boolean).join(' · ')};
    return {label, description: parts.map(part => part === 'active' ? t('Available') : /^(?:enabled|disabled|unstarted|connected|connecting|refreshing|disconnecting|suppressed|error|reconnecting|unavailable|failed|disconnected)$/.test(part) ? errorText(part) : templateText(part, '{0} tools') ?? (/^g\d+$/.test(part) ? t('Generation {0}', part.slice(1)) : part.startsWith('error=') ? t('Error: {0}', part.slice(6)) : part)).join(' · ')};
  }
  if (kind === 'sessions' && item.action) return {label, description: item.current && description.endsWith(' [active]') ? `${description.slice(0, -9)} [${t('Active session')}]` : description};
  if (kind === 'subagent_jobs' && panel.children.some(([key]) => key === (item.id ?? label))) {
    const parts = description.split(' · '); return {label, description: [errorText(parts[0]), parts[1], ...parts.slice(2)].join(' · ')};
  }
  if (processList(panel) && item.id && !['ended', 'refresh'].includes(item.id)) return {label, description: processDescription(description)};
  if (kind === 'goal' && !item.action) {
    if (label === 'Tokens') return {label: t('Tokens'), description: description.split(' · ').map(part => templateText(part, '{0} estimated requests') ?? errorText(part)).join(' · ')};
    if (label === 'Elapsed') return {label: t('Elapsed'), description: templateText(description, '{0} seconds') ?? description};
    return {label: errorText(label), description};
  }
  if (kind === 'shells' && item.action && !['auto', undefined].includes(item.action.command.selector as string | undefined)) return {label, description};
  if (kind === 'shells' && description.endsWith(' · Discover shells in this distribution')) return {label, description: description.replace(/Discover shells in this distribution$/, t('Discover shells in this distribution'))};
  if (kind === 'thinking_effort') {
    const match = /^→ (.+) via (.+)$/.exec(description);
    return {label: errorText(label), description: match ? t('API value: {0} · parameter: {1}', match[1], match[2]) : description};
  }
  return {
    label: templateText(label, 'Ended processes · {0}') ?? coreText(label),
    description: kind === 'goal' && description.startsWith('Describe an objective · Default budget: ') ? t('Describe an objective · Default budget: {0}', budgetText(description.slice('Describe an objective · Default budget: '.length))) : coreText(description),
  };
}

function budgetText(value: string): string {return templateText(value, '{0} tokens') ?? errorText(value);}

function processDescription(value: string): string {
  const parts = value.split(' · ');
  return parts.map((part, index) => index === 0 ? errorText(part) : index === 1 && /^\d+(?:\.\d+)?s$/.test(part) ? t('{0}s', part.slice(0, -1)) : part).join(' · ');
}

export function panelBody(panel: Panel): string {
  const body = panel.body ?? '';
  if (!isChinese()) return body;
  if (processPanel(panel)) {
    const lines = body.split('\n');
    const session = lines.lastIndexOf(`Session: ${panel.view_type.slice('process_session:'.length)}`);
    // The command can span multiple lines. Anchor metadata to this panel's ID,
    // and leave an unfamiliar body verbatim instead of translating command text.
    let status = -1;
    for (let index = session - 1; index > 0; index--) {
      if (/^(running|exited|unknown) · \d+(?:\.\d+)?s · .+\/(pipe|pty)$/.test(lines[index])) {status = index; break;}
    }
    if (status < 1 || session <= status) return body;
    return lines.map((line, index) => {
      if (index < status || index > status + 1 && index < session) return line;
      if (index === status) return processDescription(line);
      for (const key of ['Directory: {0}', 'Session: {0}', 'Exit code: {0}', 'Termination: {0}', 'Output preview: latest {0} characters per stream read by this interface'] as const) {
        const result = templateText(line, key); if (result !== undefined) return result;
      }
      return errorText(line);
    }).join('\n');
  }
  if (panel.view_type === 'shells') return body.split('\n').map((line, index) => index === 0 ? templateText(line, 'Current: {0}', [0]) ?? line : index === 1 ? errorText(line) : line).join('\n');
  return body;
}

export function parameterLabel(action: Action, name: string): string {
  return name === 'session_id' && action.feature_id === 'processes' ? t('Process session ID') : errorText(name);
}

export function modeLabel(name: string): string {return ['coder', 'planner', 'code'].includes(name) ? errorText(name) : name;}

export function permissionTarget(text: string): string {
  if (!isChinese() || text.startsWith('MCP · ')) return text;
  const [head, ...rest] = text.split(' · ');
  const match = /^(\w+)( \[.*\])?$/.exec(head);
  const label = ['Effect', 'Profile', 'Source'].includes(head) ? errorText(head) : match ? toolLabel(match[1]) + (match[2] ?? '') : head;
  return [label, ...rest].join(' · ');
}
