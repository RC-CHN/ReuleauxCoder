import {constants} from 'node:fs';
import {copyFile, lstat, mkdir, readdir, realpath, rm} from 'node:fs/promises';
import {basename, dirname, join, relative, isAbsolute, sep} from 'node:path';
import {t} from '../i18n.js';

/** Copy data only, on the workspace host. Never follow links or replace edits. */
export async function copySkillToWorkspace(workspace: string, name: string, location: string): Promise<string> {
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || name.length > 64 || basename(location) !== 'SKILL.md' || !(await lstat(location)).isFile()) throw new Error(t('Invalid skill directory.'));
  const source = dirname(location);
  const root = await realpath(workspace);
  const files: string[] = []; const directories: string[] = [];
  async function scan(path = ''): Promise<void> {
    const info = await lstat(join(source, path));
    if (info.isSymbolicLink() || !info.isDirectory() && !info.isFile()) throw new Error(t('Skill copies must contain regular files and directories, without symbolic links.'));
    if (info.isDirectory()) {
      if (path) directories.push(path);
      for (const entry of await readdir(join(source, path))) await scan(join(path, entry));
    } else files.push(path);
  }
  await scan();
  let parent = root;
  for (const part of ['.rcoder', 'skills']) {
    parent = join(parent, part);
    try {await mkdir(parent);} catch (error) {if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;}
    const info = await lstat(parent);
    const path = relative(root, await realpath(parent));
    if (!info.isDirectory() || info.isSymbolicLink() || path === '..' || path.startsWith(`..${sep}`) || isAbsolute(path)) throw new Error(t('The workspace skill directory must stay inside this workspace.'));
  }
  const target = join(parent, name);
  try {await mkdir(target);} catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new Error(t('A workspace skill named {0} already exists. Reload skills to use it.', name));
    throw error;
  }
  try {
    for (const path of directories) await mkdir(join(target, path));
    for (const path of files) await copyFile(join(source, path), join(target, path), constants.COPYFILE_EXCL);
    return join(target, 'SKILL.md');
  } catch (error) {
    await rm(target, {recursive: true, force: true});
    throw error;
  }
}
