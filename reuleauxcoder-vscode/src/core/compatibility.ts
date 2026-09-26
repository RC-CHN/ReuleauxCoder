import metadata from '../../package.json' with {type: 'json'};
import {t} from '../i18n.js';

export const minimumCoreVersion = metadata.version;
// Bump when editor integration needs a core fix not captured by capability flags.
export const minimumEditorApiVersion = 2;

function release(value: unknown): number[] | undefined {
  if (typeof value !== 'string') return;
  // Python distribution versions may include a post-release or local build suffix.
  const match = /^(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?(?:\+[a-z\d.-]+)?$/i.exec(value);
  if (!match) return;
  const parts = match.slice(1, 4).map(Number);
  return parts.every(Number.isSafeInteger) ? parts : undefined;
}

/** Check before runtime.ready so an incompatible core cannot resume a saved goal. */
export function compatibilityProblem(info: Record<string, unknown>): string | undefined {
  const current = release(info.core_version); const required = release(minimumCoreVersion)!;
  if (!current) return t('This core does not report a supported release version. Install the bundled core ({0} or newer).', minimumCoreVersion);
  const difference = current.map((part, index) => part - required[index]).find(value => value !== 0) ?? 0;
  if (difference < 0) return t('Core {0} is too old. This extension requires {1} or newer.', String(info.core_version), minimumCoreVersion);
  if (!Number.isSafeInteger(info.editor_api_version) || Number(info.editor_api_version) < minimumEditorApiVersion) {
    return t('Core {0} lacks editor integration revision {1}. Update the core even if the release number is unchanged.', String(info.core_version), minimumEditorApiVersion);
  }
}
