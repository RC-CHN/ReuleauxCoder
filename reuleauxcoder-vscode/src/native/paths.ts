import {basename, dirname, join, normalize, resolve} from 'node:path';
import {realpathSync} from 'node:fs';

export function pathKey(path: string): string {const value = normalize(path); return process.platform === 'win32' ? value.toLowerCase() : value;}
/** Editor URIs and Python-resolved proposals can use different Windows 8.3 names. */
export function canonicalPathKey(path: string): string {
  let ancestor = resolve(path); const missing: string[] = [];
  for (;;) {
    try {return pathKey(join(realpathSync.native(ancestor), ...missing));}
    catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error;
      const parent = dirname(ancestor);
      if (parent === ancestor) return pathKey(resolve(path));
      missing.unshift(basename(ancestor)); ancestor = parent;
    }
  }
}
