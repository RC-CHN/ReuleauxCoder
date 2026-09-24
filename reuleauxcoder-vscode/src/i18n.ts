import chinese from '../l10n/bundle.l10n.zh-cn.json' with {type: 'json'};

export type MessageKey = keyof typeof chinese;
let locale = 'en';
export function setLocale(language: string): void {locale = language.toLowerCase().startsWith('zh') ? 'zh-cn' : 'en';}
export function isChinese(): boolean {return locale === 'zh-cn';}
export function compactNumber(value: number): string {return new Intl.NumberFormat(locale, {notation: 'compact', maximumFractionDigits: 1}).format(value);}
export function shortDuration(seconds: number): string {
  const unit = seconds < 60 ? 's' : seconds < 3600 ? 'm' : 'h';
  return t(unit === 's' ? '{0}s' : unit === 'm' ? '{0}m' : '{0}h', Math.floor(seconds / (unit === 's' ? 1 : unit === 'm' ? 60 : 3600)));
}
export function translate(language: string, message: MessageKey, ...args: (string | number)[]): string {
  const text = language.toLowerCase().startsWith('zh') ? chinese[message] ?? message : message;
  return text.replace(/\{(\d+)\}/g, (token, index) => args[Number(index)] === undefined ? token : String(args[Number(index)]));
}
export function t(message: MessageKey, ...args: (string | number)[]): string {return translate(locale, message, ...args);}
const builtinTools = new Set<string>(['read_file', 'write_file', 'edit_file', 'list_file', 'glob', 'grep', 'shell', 'shell_session', 'lsp', 'lsp_status', 'lsp_diagnostics', 'lsp_restart', 'view_image', 'web_fetch', 'web_search', 'spawn_agent', 'send_message', 'list_agents', 'wait_agent', 'interrupt_agent', 'write_note', 'edit_note', 'delete_note', 'history_search', 'history_read', 'artifact_read', 'get_goal', 'create_goal', 'update_goal', 'update_plan', 'report_progress', 'report_to_parent', 'request_guidance']);
export function toolLabel(name: string): string {return builtinTools.has(name) ? errorText(name) : name;}
/** Preserve backend/model diagnostics verbatim; translate only known extension messages. */
export function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return Object.hasOwn(chinese, message) ? t(message as MessageKey) : message;
}

/** Translate known core UI templates, preserving embedded paths, names and code. */
export function coreText(text: string): string {
  if (!isChinese()) return text;
  return text.split('\n').map(line => {
    if (Object.hasOwn(chinese, line)) return errorText(line);
    let match: RegExpMatchArray | null;
    if ((match = line.match(/^Approval required: (.+)$/))) return t('Approval required: {0}', toolLabel(match[1]));
    if ((match = line.match(/^Tool '(.+)' from source '(.+)' requires approval\.$/))) return t('Tool {0} from {1} requires approval.', match[1], errorText(match[2]));
    if ((match = line.match(/^This (\d+) (files|resources)$/))) return t('These {0} resources', match[1]);
    if ((match = line.match(/^(Operation|Target|Targets|Sub-agent task|Exact external target|Workspace root): (.*)$/))) return `${errorText(match[1])}: ${match[2]}`;
    if ((match = line.match(/^Source: sub-agent \(mode=(.*)\)$/))) return t('Source: sub-agent (mode={0})', errorText(match[1]));
    if ((match = line.match(/^approved for session: (.*)$/))) return t('approved for session: {0}', coreText(match[1]));
    if (!/^(?:session:|workspace:|global:|builtin:|effective:|currently |inherits |no override|⚠ [Bb][Rr][Oo][Aa][Dd] [Ss][Cc][Oo][Pp][Ee])/.test(line)) return line;
    return line.split(' · ').map(part => {
      if ((match = part.match(/^(session|workspace|global|builtin|effective): (allow|warn|require_approval|deny|no override)$/))) return `${errorText(match[1])}: ${errorText(match[2])}`;
      if (part.startsWith('currently ')) return t('Currently {0}', errorText(part.slice(10)));
      if (part.startsWith('inherits ')) return t('Inherits {0}', errorText(part.slice(9)));
      return errorText(part);
    }).join(' · ');
  }).join('\n');
}
