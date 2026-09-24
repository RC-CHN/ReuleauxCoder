import {t} from '../i18n.js';
import {spawn} from 'node:child_process';
import {createHash, randomUUID} from 'node:crypto';
import {access, mkdir, readFile, rename, rm, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import type {CoreCommand} from './runtime.js';

export function pythonCandidates(configured: string, platform = process.platform): CoreCommand[] {
  if (configured) return [{command: configured, args: []}];
  return platform === 'win32' ? [{command: 'py', args: ['-3']}, {command: 'python', args: []}] : [{command: 'python3', args: []}, {command: 'python', args: []}];
}
export function environmentPython(directory: string, platform = process.platform): string {
  return join(directory, platform === 'win32' ? 'Scripts' : 'bin', platform === 'win32' ? 'python.exe' : 'python');
}
export async function managedCommand(storage: string): Promise<CoreCommand | undefined> {
  try {
    const data = JSON.parse(await readFile(join(storage, 'core.json'), 'utf8'));
    if (typeof data.python !== 'string') throw new Error(t('Invalid managed core installation metadata.'));
    await access(data.python);
    return {command: data.python, args: ['-m', 'reuleauxcoder']};
  } catch (error) {if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error;}
}
export async function run(command: CoreCommand, args: string[], log: (text: string) => void, signal?: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command.command, [...command.args, ...args], {stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true, shell: false, signal});
    child.stdout.on('data', data => log(data.toString())); child.stderr.on('data', data => log(data.toString()));
    let failure: Error | undefined;
    child.once('error', error => {failure = error;});
    // Abort emits error before close. Wait for the process to stop before deleting its environment.
    child.once('close', code => failure ? reject(failure) : code === 0 ? resolve() : reject(new Error(t('{0} exited with code {1}. See Reuleaux logs.', command.command, code ?? t('unknown')))));
  });
}

/** Install a packaged wheel in a private, versioned environment on this workspace host. */
export async function installCore(storage: string, bundle: string, configuredPython: string, log: (text: string) => void, signal?: AbortSignal): Promise<CoreCommand> {
  const manifest = JSON.parse(await readFile(join(bundle, 'manifest.json'), 'utf8').catch(() => {throw new Error(t('This development build has no bundled core. Run npm run bundle:core, or select an existing core from this checkout.'));}));
  if (typeof manifest.wheel !== 'string' || !/^[\w.-]+\.whl$/.test(manifest.wheel) || typeof manifest.sha256 !== 'string') throw new Error(t('Invalid bundled core manifest.'));
  const wheel = join(bundle, manifest.wheel);
  if (createHash('sha256').update(await readFile(wheel)).digest('hex') !== manifest.sha256) throw new Error(t('Bundled core checksum mismatch.'));
  let python: CoreCommand | undefined;
  for (const candidate of pythonCandidates(configuredPython)) {
    try {await run(candidate, ['-c', 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'], log, signal); python = candidate; break;}
    catch (error) {if (signal?.aborted) throw error; log(`${String(error)}\n`);}
  }
  if (!python) throw new Error(t('Python 3.10+ was not found on the workspace host. Install Python there or set reuleaux.pythonPath.'));
  const directory = join(storage, 'runtimes', `${manifest.sha256.slice(0, 12)}-${randomUUID()}`);
  await mkdir(directory, {recursive: true});
  const command = {command: environmentPython(directory), args: ['-m', 'reuleauxcoder']};
  try {
    let uv = false;
    if (!python.args.length) {
      try {await run({command: 'uv', args: []}, ['--version'], log, signal); uv = true;}
      catch (error) {if (signal?.aborted || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;}
    }
    if (uv) {
      await run({command: 'uv', args: []}, ['venv', '--python', python.command, directory], log, signal);
      await run({command: 'uv', args: []}, ['pip', 'install', '--python', command.command, wheel], log, signal);
    } else {
      await run(python, ['-m', 'venv', directory], log, signal);
      await run({command: command.command, args: []}, ['-m', 'pip', 'install', '--disable-pip-version-check', wheel], log, signal);
    }
    await run(command, ['--version'], log, signal);
    const temporary = join(storage, `core-${randomUUID()}.json`);
    await writeFile(temporary, JSON.stringify({python: command.command, sha256: manifest.sha256}), 'utf8');
    await rename(temporary, join(storage, 'core.json'));
    return command;
  } catch (error) {await rm(directory, {recursive: true, force: true}); throw error;}
}
