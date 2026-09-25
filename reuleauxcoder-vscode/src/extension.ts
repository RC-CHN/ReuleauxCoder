import {t, setLocale, errorText} from './i18n.js';
import * as vscode from 'vscode';
import {join} from 'node:path';
import {ConversationViews} from './views.js';
import {WorkspaceSession} from './core/session.js';
import {managedCommand, installCore} from './core/install.js';
import type {CoreCommand} from './core/runtime.js';
import {NativeReviews} from './native/reviews.js';
import {editorContext, diagnosticActions} from './native/context.js';
import {openWorkspaceFile} from './native/file-links.js';

let active: WorkspaceSession | undefined;
let creating: Promise<WorkspaceSession> | undefined;
let folder: vscode.WorkspaceFolder | undefined;
let installing: Promise<void> | undefined;
let installAbort: AbortController | undefined;

export function activate(context: vscode.ExtensionContext) {
  setLocale(vscode.env.language);
  const logs = vscode.window.createOutputChannel(t('Reuleaux Core'));
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 20);
  status.command = 'reuleaux.open'; status.text = '$(comment-discussion) Reuleaux'; status.show();
  const error = (reason: unknown) => {const text = errorText(reason); logs.appendLine(text); if (active) active.report(reason); else void vscode.window.showErrorMessage(text);};
  const reviews = new NativeReviews(() => views.changed(), error);
  let editorRevision = 0;
  let editorSync: Promise<unknown> = Promise.resolve();
  const dirtyPaths = () => vscode.workspace.textDocuments.filter(document => document.isDirty && ['file', 'vscode-remote'].includes(document.uri.scheme)).map(document => document.uri.fsPath);
  const syncEditors = () => {
    const client = active?.client;
    void active?.recovery.syncEditors(dirtyPaths()).catch(error);
    if (!client?.info?.editor_documents || client.peer.closed) return Promise.resolve();
    const paths = dirtyPaths();
    editorSync = client.peer.request('runtime.editor_documents', {revision: ++editorRevision, paths});
    void editorSync.catch(error);
    return editorSync;
  };
  const getSession = async (): Promise<WorkspaceSession> => {
    if (active) return active;
    if (creating) return creating;
    creating = (async () => {
      if (!vscode.workspace.isTrusted) throw new Error(t('Trust this workspace before starting its core.'));
      const folders = vscode.workspace.workspaceFolders;
      if (!folders?.length) throw new Error(t('Open a local or Remote workspace folder first.'));
      folder = folders.length === 1 ? folders[0] : await vscode.window.showWorkspaceFolderPick({placeHolder: t('Choose the workspace that will own this session')});
      if (!folder) throw new Error(t('No workspace selected.'));
      const target = vscode.env.remoteName ? `${vscode.env.remoteName}: ${folder.uri.authority || folder.name}` : t('Local');
      const session = new WorkspaceSession(folder.uri.fsPath, target); active = session;
      session.on('log', text => logs.append(text));
      session.on('client', client => reviews.bind(client));
      session.on('change', () => {
        views.changed();
        const label = {idle: t('Idle'), starting: t('Connecting…'), installing: t('Installing…'), stopping: t('Saving and stopping…'), failed: t('Failed'), ready: session.client?.state.running ? t('Running') : ''}[session.phase];
        status.text = `$(comment-discussion) Reuleaux${label ? ` · ${label}` : ''}`;
        status.tooltip = `${session.environment}\n${session.workspace}\n${session.error?.message ?? session.client?.state.model ?? ''}`;
      });
      return session;
    })().finally(() => {creating = undefined;});
    return creating;
  };
  const runtimeOptions = async (session: WorkspaceSession) => {
    const config = vscode.workspace.getConfiguration('reuleaux', folder!.uri);
    const path = config.get<string>('corePath', '').trim(); const args = config.get<string[]>('coreArguments', []);
    const managed = path ? undefined : await managedCommand(context.globalStorageUri.fsPath);
    const commands: CoreCommand[] = path ? [{command: path, args}] : [...(managed ? [managed] : []), {command: 'rcoder', args}];
    return {cwd: session.workspace, commands, beforeReady: () => syncEditors()};
  };
  const start = async () => {
    const session = await getSession();
    try {await session.start(await runtimeOptions(session));}
    catch (reason) {logs.appendLine(String(reason)); throw reason;}
  };
  const install = async () => {
    if (installing) return installing;
    installing = (async () => {
      const session = await getSession();
      if (session.phase === 'ready' || session.phase === 'starting' || session.phase === 'stopping') throw new Error(t('Save and stop the existing core before installing another version.'));
      if (session.recovery.state?.busy) throw new Error(t('Wait for configuration recovery to finish.'));
      await session.recovery.close();
      session.phase = 'installing'; session.error = undefined; session.changed();
      try {
        await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: t('Install Reuleaux core on {0}', session.environment), cancellable: true}, async (_progress, token) => {
          const controller = new AbortController(); installAbort = controller; const cancel = token.onCancellationRequested(() => controller.abort());
          try {await installCore(context.globalStorageUri.fsPath, join(context.extensionPath, 'dist', 'core'), vscode.workspace.getConfiguration('reuleaux').get('pythonPath', ''), text => logs.append(text), controller.signal);}
          finally {cancel.dispose(); installAbort = undefined;}
        });
        await vscode.workspace.getConfiguration('reuleaux', folder!.uri).update('corePath', '', vscode.ConfigurationTarget.WorkspaceFolder);
        await vscode.workspace.getConfiguration('reuleaux', folder!.uri).update('coreArguments', [], vscode.ConfigurationTarget.WorkspaceFolder);
        session.phase = 'idle'; await start();
      } catch (reason) {session.fail(reason); throw reason;}
    })().finally(() => {installing = undefined;});
    return installing;
  };
  const dispatch = async (command: string, data: Record<string, any> = {}): Promise<unknown> => {
    if (command.startsWith('configuration.')) {
      const session = await getSession();
      if (!['failed', 'idle'].includes(session.phase)) throw new Error(t('Stop the core before opening configuration recovery.'));
      switch (command) {
        case 'configuration.open': await session.recovery.open(await runtimeOptions(session)); return session.recovery.syncEditors(dirtyPaths());
        case 'configuration.check': return session.recovery.check();
        case 'configuration.select': return session.recovery.select(data.id);
        case 'configuration.test': return session.recovery.testModels(data.id);
        case 'configuration.apply': return session.recovery.apply(data.id, data.offline === true, dirtyPaths());
        case 'configuration.close': if (session.recovery.state?.busy) throw new Error(t('Wait for configuration recovery to finish.')); return session.recovery.close();
        case 'configuration.file': {
          const path = session.recovery.source(data.scope);
          const uri = folder!.uri.with({path: vscode.Uri.file(path).path});
          const edit = new vscode.WorkspaceEdit(); edit.createFile(uri, {ignoreIfExists: true});
          if (!await vscode.workspace.applyEdit(edit)) throw new Error(t('Could not open the configuration file.'));
          return vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), {preview: false});
        }
      }
      throw new Error(t('Unknown view action.'));
    }
    switch (command) {
      case 'openEditor': views.openEditor(); return;
      case 'logs': logs.show(true); return;
      case 'git': return vscode.commands.executeCommand('workbench.view.scm');
      case 'start': return start();
      case 'install': return install();
      case 'selectCore': {
        if (active && ['ready', 'starting', 'stopping'].includes(active.phase)) throw new Error(t('Save and stop the existing core before installing another version.'));
        if (active?.recovery.state?.busy) throw new Error(t('Wait for configuration recovery to finish.'));
        await active?.recovery.close();
        const selected = await vscode.window.showOpenDialog({title: t('Select rcoder on the workspace host'), canSelectMany: false, canSelectFiles: true, canSelectFolders: false});
        if (!selected?.[0]) return;
        await getSession();
        await vscode.workspace.getConfiguration('reuleaux', folder!.uri).update('corePath', selected[0].fsPath, vscode.ConfigurationTarget.WorkspaceFolder);
        return start();
      }
      case 'review': return reviews.open(data.id, data.documentId);
      case 'approve': return reviews.decide(true, data.id, data.scopeId);
      case 'reject': return reviews.decide(false, data.id, undefined, data.feedback);
      case 'saveReview': return reviews.saveAndRepropose(data.id);
      case 'openLink': {
        if (typeof data.url !== 'string' || !/^https?:\/\//i.test(data.url)) throw new Error(t('Unsupported link.'));
        return vscode.env.openExternal(vscode.Uri.parse(data.url));
      }
      case 'openFile': await getSession(); return openWorkspaceFile(folder!, data.path);
      case 'addUri': {
        if (typeof data.uri !== 'string') throw new Error(t('Invalid workspace file.'));
        const uri = vscode.Uri.parse(data.uri); const session = await getSession();
        if (vscode.workspace.getWorkspaceFolder(uri)?.uri.toString() !== folder!.uri.toString()) throw new Error(t('This file is outside the selected workspace. Use the local file upload button instead.'));
        session.add(await editorContext(uri)); return;
      }
    }
    const session = await getSession(); const client = session.requireClient();
    switch (command) {
      case 'stop': return client.peer.request('runtime.stop');
      case 'newSession': return client.submitAction('sessions.new');
      case 'sessions': return session.commands.open('sessions.list');
      case 'actions': await vscode.commands.executeCommand('reuleaux.chat.focus'); return views.showCommands();
      case 'models': return session.commands.open('model.show');
      default: throw new Error(t('Unknown Reuleaux command.'));
    }
  };
  const views = new ConversationViews(context.extensionUri, getSession, reviews, dispatch);
  const register = (name: string, run: (...args: any[]) => unknown) => context.subscriptions.push(vscode.commands.registerCommand(`reuleaux.${name}`, (...args: any[]) => Promise.resolve().then(() => run(...args)).catch(error)));
  register('open', () => vscode.commands.executeCommand('reuleaux.chat.focus'));
  for (const name of ['start', 'install', 'selectCore', 'logs', 'stop', 'openEditor', 'newSession', 'sessions', 'actions']) register(name, () => dispatch(name));
  register('shutdown', async () => {await (await getSession()).shutdown();});
  register('review', (id, documentId) => reviews.open(id, documentId));
  register('approve', uri => reviews.decide(true, uri)); register('reject', uri => reviews.decide(false, uri));
  const addContext = async (uri?: vscode.Uri, range?: vscode.Range, diagnostics?: readonly vscode.Diagnostic[]) => {
    const session = await getSession();
    uri ??= vscode.window.activeTextEditor?.document.uri;
    if (!uri || vscode.workspace.getWorkspaceFolder(uri)?.uri.toString() !== folder!.uri.toString()) throw new Error(t('This file is outside the selected workspace. Use the local file upload button instead.'));
    const item = await editorContext(uri, range, diagnostics); session.add(item);
    await vscode.commands.executeCommand('reuleaux.chat.focus');
  };
  register('addContext', (uri?: vscode.Uri) => addContext(uri));
  register('explainSelection', async () => {await addContext(); active!.insertDraft(t('Explain this code.'));});
  register('fixDiagnostic', (uri, range, diagnostics) => addContext(uri, range, diagnostics));
  context.subscriptions.push(logs, status, reviews, views,
    vscode.window.registerWebviewViewProvider('reuleaux.chat', views),
    vscode.workspace.registerTextDocumentContentProvider('reuleaux-review', reviews),
    vscode.languages.registerCodeActionsProvider([{scheme: 'file'}, {scheme: 'vscode-remote'}], diagnosticActions, {providedCodeActionKinds: [vscode.CodeActionKind.QuickFix]}),
    vscode.workspace.onDidChangeTextDocument(() => {void syncEditors();}),
    vscode.workspace.onDidSaveTextDocument(() => {void syncEditors();}),
    vscode.workspace.onDidCloseTextDocument(() => {void syncEditors();}),
    vscode.workspace.onDidChangeWorkspaceFolders(event => {if (folder && event.removed.some(item => item.uri.toString() === folder!.uri.toString())) {active?.dispose(); active = undefined; folder = undefined; views.changed();}}),
    {dispose: () => {active?.dispose(); active = undefined;}},
  );
  return {getSession, reviews, views, dispatch};
}

export async function deactivate(): Promise<void> {
  installAbort?.abort();
  await installing?.catch(() => {});
  await active?.recovery.close();
  if (active?.phase === 'ready') await active.shutdown();
  active?.dispose(); active = undefined;
}
