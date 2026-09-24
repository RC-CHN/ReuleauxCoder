import chinese from '../l10n/bundle.l10n.zh-cn.json' with {type: 'json'};

export type MessageKey = keyof typeof chinese;
let locale = 'en';
export function setLocale(language: string): void {locale = language.toLowerCase().startsWith('zh') ? 'zh-cn' : 'en';}
export function translate(language: string, message: MessageKey, ...args: (string | number)[]): string {
  const text = language.toLowerCase().startsWith('zh') ? chinese[message] ?? message : message;
  return text.replace(/\{(\d+)\}/g, (token, index) => args[Number(index)] === undefined ? token : String(args[Number(index)]));
}
export function t(message: MessageKey, ...args: (string | number)[]): string {return translate(locale, message, ...args);}
/** Preserve backend/model diagnostics verbatim; translate only known extension messages. */
export function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return Object.hasOwn(chinese, message) ? t(message as MessageKey) : message;
}

/** Translate known core UI templates, preserving embedded paths, names and code. */
export function coreText(text: string): string {
  return text.split('\n').map(line => {
    if (Object.hasOwn(chinese, line)) return errorText(line);
    let match: RegExpMatchArray | null;
    if ((match = line.match(/^Approval required: (.+)$/))) return t('Approval required: {0}', errorText(match[1]));
    if ((match = line.match(/^Tool '(.+)' from source '(.+)' requires approval\.$/))) return t('Tool {0} from {1} requires approval.', match[1], errorText(match[2]));
    if ((match = line.match(/^This (\d+) (files|resources)$/))) return t('These {0} resources', match[1]);
    if ((match = line.match(/^(Operation|Target|Targets|Sub-agent task|Exact external target|Workspace root): (.*)$/))) return `${errorText(match[1])}: ${match[2]}`;
    return line.split(' · ').map(part => {
      if ((match = part.match(/^(session|workspace|global|builtin|effective): (allow|warn|require_approval|deny|no override)$/))) return `${errorText(match[1])}: ${errorText(match[2])}`;
      if (part.startsWith('currently ')) return t('Currently {0}', errorText(part.slice(10)));
      if (part.startsWith('inherits ')) return t('Inherits {0}', errorText(part.slice(9)));
      return errorText(part);
    }).join(' · ');
  }).join('\n');
}
