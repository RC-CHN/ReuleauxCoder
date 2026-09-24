import * as vscode from 'vscode';
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import type {activate} from '../../src/extension.js';
import {answerInteraction} from '../../src/core/interactions.js';

async function until(predicate: () => unknown | Promise<unknown>, timeout = 10000): Promise<void> {
  const end = Date.now() + timeout;
  while (!await predicate()) {if (Date.now() > end) throw new Error('VS Code integration condition timed out.'); await delay(25);}
}
export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension<ReturnType<typeof activate>>('RC-CHN.reuleauxcoder');
  assert(extension, 'Extension is registered');
  const api = await extension.activate();
  const manifest = extension.packageJSON;
  assert.equal(manifest.extensionKind[0], 'workspace');
  assert.equal(manifest.contributes.viewsContainers.secondarySidebar[0].id, 'reuleaux');
  await vscode.commands.executeCommand('reuleaux.start');
  const session = await api.getSession(); const client = session.requireClient();
  const uri = vscode.Uri.joinPath(vscode.workspace.workspaceFolders![0].uri, 'example.py');
  let previousReviewUri: vscode.Uri | undefined;
  try {
    for (const newline of ['\n', '\r\n']) {
      await writeFile(uri.fsPath, `old = 1${newline}`);
      session.submit(`native-edit-${newline.length}`, 'edit', [], client.state.session_generation);
      await until(() => vscode.window.tabGroups.all.flatMap(group => group.tabs).some(tab => tab.input instanceof vscode.TabInputTextDiff));
      const input = vscode.window.tabGroups.all.flatMap(group => group.tabs).find(tab => tab.input instanceof vscode.TabInputTextDiff)!.input as vscode.TabInputTextDiff;
      assert.equal(input.modified.scheme, 'reuleaux-review');
      previousReviewUri = input.modified;
      assert.equal((await vscode.workspace.openTextDocument(input.original)).getText(), `old = 1${newline}`);
      assert.equal((await vscode.workspace.openTextDocument(input.modified)).getText(), `new = 1${newline}`);
      assert.equal(await readFile(uri.fsPath, 'utf8'), `old = 1${newline}`);
      await vscode.commands.executeCommand('reuleaux.approve', input.modified);
      await until(async () => (await readFile(uri.fsPath, 'utf8')) === `new = 1${newline}`);
      await until(() => !client.state.running);
      assert.equal(client.interactions.length, 0);
      // Expired editor buttons must never approve a later request.
      await vscode.commands.executeCommand('reuleaux.approve', input.modified);
      await vscode.window.tabGroups.close(vscode.window.tabGroups.all.flatMap(group => group.tabs).filter(tab => tab.input instanceof vscode.TabInputTextDiff));
      console.log(`PASS native readonly diff, approval and expired actions (${newline.length === 1 ? 'LF' : 'CRLF'})`);
    }

    const document = await vscode.workspace.openTextDocument(uri);
    let editor = await vscode.window.showTextDocument(document);
    assert.equal(document.eol, vscode.EndOfLine.CRLF);
    await editor.edit(edit => edit.replace(new vscode.Range(0, 0, document.lineCount, 0), 'old = 1\r\n'));
    await document.save();
    await editor.edit(edit => edit.replace(new vscode.Range(0, 0, document.lineCount, 0), 'old = 1\r\n# unsaved\r\n'));
    assert(document.isDirty);
    await until(() => client.peer.request('test.is_dirty', {path: uri.fsPath}));
    session.submit('dirty-edit', 'auto-edit', [], client.state.session_generation);
    await until(() => session.transcript.cells.some(cell => cell.text.includes('Unsaved editor changes')));
    assert.equal(await readFile(uri.fsPath, 'utf8'), 'old = 1\r\n');
    assert.equal(document.getText(), 'old = 1\r\n# unsaved\r\n');
    await document.save();
    await until(async () => !await client.peer.request('test.is_dirty', {path: uri.fsPath}));
    console.log('PASS real unsaved editor synchronization and automatic edit guard');

    await until(() => !client.state.running);
    session.submit('review-dirty-after-proposal', 'edit', [], client.state.session_generation);
    await until(() => client.interactions.some(item => item.kind === 'review'));
    const reviewId = client.interactions.find(item => item.kind === 'review')!.request.request_id;
    api.reviews.bind(client);
    await api.reviews.decide(true, previousReviewUri);
    assert(client.interactions.some(item => item.request.request_id === reviewId), 'An old editor URI must not approve a replacement review');
    const pendingEdit = new vscode.WorkspaceEdit(); pendingEdit.insert(uri, new vscode.Position(0, 0), '# changed after proposal\r\n');
    assert(await vscode.workspace.applyEdit(pendingEdit));
    assert(api.reviews.summaries().find(item => item.id === reviewId)?.dirty);
    await assert.rejects(api.reviews.decide(true, reviewId), /unsaved editor/);
    await assert.rejects(api.reviews.decide(true, reviewId, 'invented-scope'), /expired/);
    await api.reviews.saveAndRepropose(reviewId);
    await until(() => !client.state.running);
    assert((await readFile(uri.fsPath, 'utf8')).startsWith('# changed after proposal\r\nold = 1'));
    assert.equal(client.interactions.length, 0);
    console.log('PASS inline dirty-review handling rejects stale writes and validates grant IDs');

    editor = await vscode.window.showTextDocument(document);
    editor.selection = new vscode.Selection(1, 0, 1, 3);
    await vscode.commands.executeCommand('reuleaux.addContext', uri);
    assert(session.draftItems.some(item => item.text?.includes('old')));
    const collection = vscode.languages.createDiagnosticCollection('reuleaux-test');
    collection.set(uri, [new vscode.Diagnostic(new vscode.Range(0, 0, 0, 3), 'test diagnostic')]);
    const codeActions = await vscode.commands.executeCommand<vscode.CodeAction[]>('vscode.executeCodeActionProvider', uri, new vscode.Range(0, 0, 0, 3));
    assert(codeActions?.some(action => action.command?.command === 'reuleaux.fixDiagnostic'));
    collection.dispose();
    await vscode.commands.executeCommand('reuleaux.openEditor');
    await until(() => vscode.window.tabGroups.all.flatMap(group => group.tabs).some(tab => tab.input instanceof vscode.TabInputWebview));
    const tabs = vscode.window.tabGroups.all.flatMap(group => group.tabs).filter(tab => tab.input instanceof vscode.TabInputWebview);
    await vscode.window.tabGroups.close(tabs);
    assert.equal((await api.getSession()).requireClient(), client);
    assert(!client.peer.closed);
    console.log('PASS editor context, diagnostic action and view-independent connection');

    await until(() => !client.state.running);
    session.submit('native-question', 'question', [], client.state.session_generation);
    await until(() => client.interactions.some(item => item.kind === 'input_text'));
    assert(session.snapshot().interactions![0].secret);
    answerInteraction(client, {id: client.interactions[0].request.request_id, value: 'inline answer'});
    await until(() => !client.state.running);
    session.submit('cancel-question', 'question', [], client.state.session_generation);
    await until(() => client.interactions.some(item => item.kind === 'input_text'));
    await client.interrupt();
    await until(() => !client.interactions.length && !client.state.running);
    console.log('PASS inline secret answer and backend cancellation');
  } finally {await client.interrupt(); await session.shutdown();}
}
