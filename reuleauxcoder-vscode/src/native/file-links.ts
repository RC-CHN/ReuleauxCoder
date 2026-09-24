import * as vscode from 'vscode';
import {resolve, relative, isAbsolute, sep} from 'node:path';
import {realpath} from 'node:fs/promises';
import {fileReference} from '../file-links.js';
import {t} from '../i18n.js';

function within(root: string, target: string): boolean {
  const path = relative(root, target); return path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path);
}

export async function openWorkspaceFile(folder: vscode.WorkspaceFolder, input: unknown): Promise<void> {
  const reference = typeof input === 'string' ? fileReference(input) : undefined;
  if (!reference) throw new Error(t('Invalid file reference.'));
  const root = folder.uri.fsPath;
  const target = resolve(root, reference.path.replaceAll('\\', sep));
  if (!within(root, target)) throw new Error(t('This link points outside the current workspace.'));
  let canonical: string;
  try {canonical = await realpath(target);} catch {throw new Error(t('File not found in this workspace: {0}', reference.path));}
  if (!within(await realpath(root), canonical)) throw new Error(t('This link points outside the current workspace.'));
  // Retain the selected workspace's Remote authority rather than opening a
  // similarly named file on the UI machine.
  const uri = vscode.Uri.joinPath(folder.uri, ...relative(root, target).split(sep)).with({query: '', fragment: ''});
  if ((await vscode.workspace.fs.stat(uri)).type & vscode.FileType.Directory) {await vscode.commands.executeCommand('revealInExplorer', uri); return;}
  const document = await vscode.workspace.openTextDocument(uri);
  const selection = reference.line ? new vscode.Range(
    document.validatePosition(new vscode.Position(reference.line - 1, (reference.column ?? 1) - 1)),
    document.validatePosition(new vscode.Position((reference.endLine ?? reference.line) - 1, reference.endLine !== reference.line ? Number.MAX_SAFE_INTEGER : (reference.column ?? 1) - 1)),
  ) : undefined;
  await vscode.window.showTextDocument(document, {viewColumn: vscode.ViewColumn.One, preview: true, selection});
}
