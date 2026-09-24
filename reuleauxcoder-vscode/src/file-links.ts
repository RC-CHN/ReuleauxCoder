export interface FileReference {path: string; line?: number; column?: number; endLine?: number}

const extensions = 'md|mdx|txt|rst|ts|tsx|js|jsx|mjs|cjs|py|pyi|json|jsonc|yaml|yml|toml|ini|cfg|conf|lock|go|rs|java|kt|swift|c|h|cc|cpp|hpp|cs|rb|php|sh|bash|zsh|ps1|bat|cmd|sql|css|scss|sass|less|html|htm|vue|svelte|xml|svg|csv|ipynb';
const filename = new RegExp(`(?:\\.(?:${extensions})|(?:^|[/\\\\])(?:Dockerfile|Makefile|LICENSE|README|\\.gitignore|\\.gitattributes|\\.editorconfig|\\.env))$`, 'i');

/** A file reference is data, never a command or an external URI. */
export function fileReference(value: string): FileReference | undefined {
  if (value.length > 4096 || /[\u0000-\u001f]/.test(value)) return;
  let path = value.trim(); let line: number | undefined, column: number | undefined, endLine: number | undefined;
  const suffix = path.match(/(?::(\d+)(?::(\d+))?(?:-(\d+))?|#L(\d+)(?:C(\d+))?(?:-L?(\d+))?)$/i);
  if (suffix) {
    path = path.slice(0, -suffix[0].length);
    line = Number(suffix[1] ?? suffix[4]); column = Number(suffix[2] ?? suffix[5] ?? 1); endLine = Number(suffix[3] ?? suffix[6] ?? line);
    if (![line, column, endLine].every(number => Number.isSafeInteger(number) && number > 0 && number <= 2147483647) || endLine < line) return;
  } else path = path.split('#', 1)[0];
  try {path = decodeURIComponent(path);} catch {return;}
  if (!path || /[\u0000-\u001f<>|?*]/.test(path) || /^[a-z][a-z\d+.-]*:/i.test(path) && !/^[a-z]:[/\\]/i.test(path)) return;
  if (!filename.test(path) && !path.endsWith('/')) return;
  return {path, ...(line ? {line, column, endLine} : {})};
}

// Explicit Markdown links may contain spaces; automatic references stay within
// one prose token so command snippets such as `cat README.md` are not file names.
export const fileReferences = new RegExp(`(?<![\\p{L}\\p{N}_@./\\\\-])(?:[A-Za-z]:[/\\\\]|\\.{1,2}[/\\\\]|/)?(?:[\\p{L}\\p{N}_@.-]+[/\\\\])*[\\p{L}\\p{N}_@.-]+\\.(?:${extensions})(?::\\d+(?::\\d+)?(?:-\\d+)?|#L\\d+(?:C\\d+)?(?:-L?\\d+)?)?(?![A-Za-z0-9_/\\\\-]|\\.[A-Za-z0-9_])`, 'giu');
