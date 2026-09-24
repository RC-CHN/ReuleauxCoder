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
