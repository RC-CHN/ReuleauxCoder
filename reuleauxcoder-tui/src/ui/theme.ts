import sliceAnsi from 'slice-ansi';
import stringWidth from 'string-width';

export interface Theme {
  accent: string;
  secondary: string;
  info: string;
  border: string;
  foreground: string;
  background: string;
  panelBackground: string;
  additionBackground: string;
  deletionBackground: string;
  muted: string;
  success: string;
  warning: string;
  error: string;
  selectionBackground: string;
  selectionText: string;
}

const terminal: Theme = {
  accent: 'cyan', secondary: 'magenta', info: 'blue', border: 'gray', foreground: 'default', background: 'default',
  panelBackground: 'default', additionBackground: 'default', deletionBackground: 'default',
  muted: 'default', success: 'green', warning: 'yellow', error: 'red',
  selectionBackground: 'blue', selectionText: 'white',
};
export const DEFAULT_THEME = 'workbench';
export const presets: Record<string, Theme> = {
  terminal,
  workbench: {
    ...terminal,
    accent: '#dbac6a', secondary: '#93b8ac', info: '#9fbad6', border: '#46524f', foreground: '#dedcd3', background: '#191d1e',
    muted: '#919b98', success: '#93b8ac', warning: '#e4b976', error: '#e58c7d',
    panelBackground: '#242c2a', additionBackground: '#24382f', deletionBackground: '#3c2b2a',
    selectionBackground: '#35423e', selectionText: '#f1e5cd',
  },
  ocean: {
    ...terminal, secondary: '#bb9af7', info: '#7dcfff', border: '#46536c',
    accent: '#7dcfff', muted: '#8493aa', success: '#9ece6a', warning: '#e0af68',
    error: '#f7768e', selectionBackground: '#253a59', selectionText: '#c0e5ff',
  },
  ember: {
    ...terminal, secondary: '#9bc5b5', info: '#a6bacd', border: '#65544a',
    accent: '#eab676', muted: '#a5988c', success: '#a8ba83', warning: '#e6bf72',
    error: '#ed8a80', selectionBackground: '#493329', selectionText: '#fff0da',
  },
};

const ansiColors: Record<string, number> = {
  black: 30, red: 31, green: 32, yellow: 33, blue: 34,
  magenta: 35, cyan: 36, white: 37, gray: 90, default: 39,
};
const isColor = (value: unknown): value is string => typeof value === 'string' &&
  (Object.hasOwn(ansiColors, value) || /^#[\da-f]{6}$/i.test(value));

/** Validate configuration once, before starting the backend or taking over the terminal. */
export function resolveTheme(value: unknown): Theme {
  if (typeof value === 'string') {
    if (!Object.hasOwn(presets, value)) throw new Error(`Unknown theme: ${value}`);
    return {...presets[value]};
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Theme must be a preset name or an object');
  const {extends: base = 'terminal', ...colors} = value as Record<string, unknown>;
  if (typeof base !== 'string') throw new Error('Theme extends must be a preset name');
  const theme = resolveTheme(base);
  for (const [key, color] of Object.entries(colors)) {
    if (!Object.hasOwn(theme, key)) throw new Error(`Unknown theme color: ${key}`);
    if (!isColor(color)) throw new Error(`Invalid ${key} color: use #RRGGBB or a terminal color name`);
    theme[key as keyof Theme] = color;
  }
  return theme;
}

let current = presets[DEFAULT_THEME];
export function configureTheme(theme: Theme) {current = {...theme};}

function colorCode(color: string, background = false): string {
  if (Object.hasOwn(ansiColors, color)) return `\x1b[${ansiColors[color] + (background ? 10 : 0)}m`;
  const rgb = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16));
  return `\x1b[${background ? 48 : 38};2;${rgb.join(';')}m`;
}

const foreground = (role: keyof Theme, text: string) => colorCode(current[role]) + text + '\x1b[39m';
export type AccentRole = 'accent' | 'secondary' | 'info' | 'success' | 'warning' | 'error';
// Restore enclosing colors after nested Markdown, selections and cursor styles.
function surface(text: string, foregroundColor: string, backgroundColor: string): string {
  const fg = colorCode(foregroundColor), bg = colorCode(backgroundColor, true);
  return fg + bg + text.replaceAll('\x1b[39m', fg).replaceAll('\x1b[49m', bg).replaceAll('\x1b[0m', '\x1b[0m' + fg + bg) + '\x1b[39m\x1b[49m';
}
const badge = (text: string, role: AccentRole = 'accent') => surface(` ${text} `, current.background === 'default' ? 'black' : current.background, current[role]);
/** Shade a border with the active theme; named ANSI colors use discrete levels. */
function borderGlow(text: string, role: AccentRole, strength: number): string {
  const from = current.border, to = current[role];
  if (strength <= 0) return foreground('border', text);
  if (!from.startsWith('#') || !to.startsWith('#')) {
    const colored = foreground(role, text);
    return strength < 0.65 ? `\x1b[2m${colored}\x1b[22m` : colored;
  }
  const rgb = [1, 3, 5].map(offset => {
    const start = parseInt(from.slice(offset, offset + 2), 16);
    const end = parseInt(to.slice(offset, offset + 2), 16);
    return Math.round(start + (end - start) * strength);
  });
  return `\x1b[38;2;${rgb.join(';')}m${text}\x1b[39m`;
}
export const paint = {
  borderGlow,
  // The disappearing edge blends into the configured surface between row moves.
  fade: (text: string, visibility: number) => {
    if (visibility >= 1) return text;
    if (!current.background.startsWith('#')) return visibility < 0.5 ? `\x1b[2m${text}\x1b[22m` : text;
    const background = [1, 3, 5].map(offset => parseInt(current.background.slice(offset, offset + 2), 16));
    return text.replace(/\x1b\[38;2;(\d+);(\d+);(\d+)m/g, (_code, r, g, b) =>
      `\x1b[38;2;${[r, g, b].map((value, index) => Math.round(background[index] + (Number(value) - background[index]) * visibility)).join(';')}m`);
  },
  // Only brighten existing truecolor foregrounds. Preserve backgrounds, dark
  // badge text and ANSI palette colors; content is fully present from frame one.
  reveal: (text: string, progress: number) => text.replace(/\x1b\[38;2;(\d+);(\d+);(\d+)m/g, (code, r, g, b) => {
    const rgb = [Number(r), Number(g), Number(b)];
    if (Math.max(...rgb) < 140 || progress === 1) return code;
    return `\x1b[38;2;${rgb.map(value => Math.round(value * (0.78 + 0.22 * progress))).join(';')}m`;
  }),
  dim: (text: string) => `\x1b[2m${text.replaceAll('\x1b[22m', '\x1b[22m\x1b[2m')}\x1b[22m`,
  muted: (text: string) => current.muted === 'default' ? `\x1b[2m${text}\x1b[22m` : foreground('muted', text),
  bold: (text: string) => `\x1b[1m${text}\x1b[22m`,
  accent: (text: string) => foreground('accent', text),
  secondary: (text: string) => foreground('secondary', text),
  info: (text: string) => foreground('info', text),
  border: (text: string) => foreground('border', text),
  success: (text: string) => foreground('success', text),
  error: (text: string) => foreground('error', text),
  warning: (text: string) => foreground('warning', text),
  selected: (text: string) => colorCode(current.selectionBackground, true) + colorCode(current.selectionText) + text + '\x1b[39m\x1b[49m',
  badge,
  action: (text: string) => badge(text, 'success'),
  panel: (text: string) => surface(text, current.foreground, current.panelBackground),
  addition: (text: string) => surface(text, current.success, current.additionBackground),
  deletion: (text: string) => surface(text, current.error, current.deletionBackground),
  surface: (text: string) => surface(text, current.foreground, current.background),
};

const fittedRows = new Map<string, string>();
let fittedChars = 0;

export function fit(text: string, width: number): string {
  // Adjacent scroll frames share most rows. Keep exact ANSI clipping semantics,
  // while bounding both cache entries and retained string storage.
  const key = text.length <= 4096 ? `${width}\0${text}` : undefined;
  if (key !== undefined) {
    const cached = fittedRows.get(key);
    if (cached !== undefined) {fittedRows.delete(key); fittedRows.set(key, cached); return cached;}
  }
  const clipped = sliceAnsi(text, 0, Math.max(0, width));
  const result = clipped + ' '.repeat(Math.max(0, width - stringWidth(clipped)));
  if (key !== undefined && result.length <= 4096) {
    fittedRows.set(key, result); fittedChars += key.length + result.length;
    while (fittedRows.size > 1024 || fittedChars > 1024 * 1024) {
      const [oldKey, oldValue] = fittedRows.entries().next().value!;
      fittedRows.delete(oldKey); fittedChars -= oldKey.length + oldValue.length;
    }
  }
  return result;
}

export function between(left: string, right: string, width: number): string {
  const remaining = width - stringWidth(right) - 2;
  return remaining > 0 ? fit(left, remaining) + '  ' + right : fit(right, width);
}

export function section(label: string, detail: string, width: number, color = paint.accent, ruleColor = paint.border): string {
  const heading = label ? sliceAnsi(`─ ${label} `, 0, Math.max(0, width - 2)) : '';
  const available = Math.max(0, width - stringWidth(heading) - 3);
  const caption = sliceAnsi(detail, 0, available);
  const rule = '─'.repeat(Math.max(0, width - stringWidth(heading) - stringWidth(caption) - (caption ? 1 : 0)));
  return color(heading) + ruleColor(rule) + (caption ? paint.muted(' ' + caption) : '');
}

export function frameEdge(label: string, width: number, bottom = false, color = paint.accent, detail = '', ruleColor = paint.border): string {
  return paint.border(bottom ? '└' : '┌') + section(label, detail, width - 2, color, ruleColor) + paint.border(bottom ? '┘' : '┐');
}

export function frameRow(text: string, width: number): string {
  return paint.border('│ ') + fit(text, width - 4) + paint.border(' │');
}

export function rail(text: string, width: number, color = paint.muted): string {
  return color('▏') + ' ' + fit(text, width - 2);
}

export const keyHint = (key: string, label: string, color = paint.accent) => `${color(paint.bold(key))} ${paint.muted(label)}`;
