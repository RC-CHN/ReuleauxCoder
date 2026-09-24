import {setLocale, t, errorText, type MessageKey} from '../i18n.js';
import type {ChatCell, HostSnapshot, WebRequest} from '../shared.js';
import {decorateIcons, icon} from './icons.js';
import {renderMarkdown} from './markdown.js';
import {ComposerWorkbench} from './workbench.js';
import {AttentionCards} from './attention.js';
import {WorkOverviewView} from './overview.js';

interface SavedView {draft?: string; outbox?: {input: Omit<LocalSend, 'uploads'>; cell: ChatCell}[]}
declare function acquireVsCodeApi(): {postMessage(message: unknown): void; getState(): SavedView | undefined; setState(state: unknown): void};
const vscode = acquireVsCodeApi();
setLocale(document.documentElement.lang);
const element = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const composer = element<HTMLTextAreaElement>('composer');
const transcript = element('transcript');
decorateIcons();
const workbench = new ComposerWorkbench(element('workbench'), composer, request, notice, saveDraft);
const attention = new AttentionCards(element('reviews'), request);
const overview = new WorkOverviewView(element('overview'), element('goal-strip'), element<HTMLButtonElement>('overview-toggle'), request, notice);
const pending = new Map<string, {resolve(value: any): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout>}>();
const nodes = new Map<string, HTMLElement>();
const optimistic = new Map<string, ChatCell>();
interface LocalSend {id: string; text: string; items: string[]; generation: number; hostId: string; uploads: LocalUpload[]; busy?: boolean; cancelled?: boolean; needsFiles?: boolean}
const localSends = new Map<string, LocalSend>();
const consumedItems = new Set<string>();
let snapshot: HostSnapshot | undefined;
let initial = true;
let next = 0;
let persisted = '';
let uploadQueue = Promise.resolve();
interface LocalUpload {id: string; file: File; progress: number; cancelled: boolean; backendId?: string; error?: string; submission?: string; attachmentId?: string}
const uploads = new Map<string, LocalUpload>();

function request(action: string, data: WebRequest['data'] = {}): Promise<any> {
  const id = `view-${++next}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error(t('The view request timed out. Check the core status before retrying.')));}, action === 'install' ? 15 * 60 * 1000 : 45000);
    pending.set(id, {resolve, reject, timer}); vscode.postMessage({id, action, data: {hostId: snapshot?.hostId, generation: snapshot?.generation, ...data}});
  });
}
function notice(error: unknown): void {element('notice').textContent = errorText(error); element('notice').hidden = !element('notice').textContent;}
function persist(): void {
  const value: SavedView = {draft: composer.value, outbox: [...localSends.values()].filter(input => optimistic.has(input.id)).map(input => ({input: {id: input.id, text: input.text, items: input.items, generation: input.generation, hostId: input.hostId, needsFiles: input.needsFiles || input.uploads.length > 0}, cell: optimistic.get(input.id)!}))};
  const encoded = JSON.stringify(value); if (encoded !== persisted) {persisted = encoded; vscode.setState(value);}
}
let measuredDraft = '', measuredWidth = -1;
function resizeComposer(): void {
  if (measuredDraft === composer.value && measuredWidth === composer.clientWidth) return;
  measuredDraft = composer.value; measuredWidth = composer.clientWidth;
  composer.style.height = 'auto'; composer.style.height = `${composer.scrollHeight}px`;
}
new ResizeObserver(resizeComposer).observe(composer);
function saveDraft(): void {resizeComposer(); persist(); void request('draft', {text: composer.value}).catch(notice);}
function button(text: string, action: () => void, title = text): HTMLButtonElement {const node = document.createElement('button'); node.textContent = text; node.title = title; node.addEventListener('click', action); return node;}
function cellNode(cell: ChatCell): HTMLElement {
  let node = nodes.get(cell.id);
  if (!node) {node = document.createElement('article'); node.className = `cell ${cell.role}`; node.dataset.id = cell.id; nodes.set(cell.id, node); transcript.insertBefore(node, element('reviews'));}
  const content = JSON.stringify(cell); if (node.dataset.content === content) return node;
  const expanded = node.querySelector('details')?.open ?? false;
  node.dataset.content = content; node.replaceChildren();
  const meta = document.createElement('div'); meta.className = 'meta';
  meta.textContent = cell.role === 'user' ? t('You') : cell.role === 'reasoning' ? t('Reasoning') : cell.role === 'tool' ? errorText(cell.title ?? t('Tool')) : cell.role === 'notice' ? t('Notice') : 'Reuleaux';
  if (cell.status && cell.status !== 'applied') {const state = document.createElement('span'); state.textContent = t(cell.status as MessageKey) ?? cell.status; meta.append(state);}
  node.append(meta);
  const body = document.createElement('div'); body.className = 'body';
  if (cell.role === 'assistant' || cell.role === 'reasoning') renderMarkdown(body, cell.text, url => void request('openLink', {url}).catch(notice), path => void request('openFile', {path}).catch(notice));
  else body.textContent = cell.text;
  if (['tool', 'reasoning'].includes(cell.role)) {const details = document.createElement('details'); details.open = expanded; const summary = document.createElement('summary'); summary.textContent = errorText(cell.title ?? t('Reasoning')); details.append(summary, body); node.append(details);} else node.append(body);
  if (cell.detail) {const detail = document.createElement('div'); detail.className = 'detail'; detail.textContent = cell.detail; (cell.role === 'tool' ? node.querySelector('details')! : node).append(detail);}
  if (['unconfirmed', 'rejected'].includes(cell.status ?? '')) {const retry = button(t('Retry'), () => void retrySend(cell.id)); retry.className = 'retry'; node.append(retry);}
  if (cell.status === 'uploading' || cell.status === 'rejected' && (localSends.get(cell.id)?.uploads.length || localSends.get(cell.id)?.needsFiles)) node.append(button(t('Return to draft'), () => restoreSend(cell.id)));
  return node;
}
function render(state: HostSnapshot): void {
  if (snapshot && state.revision < snapshot.revision) return;
  const follow = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 70;
  if (snapshot && (snapshot.hostId !== state.hostId || snapshot.generation !== state.generation)) {
    // Preserve unsent input visibly, but never replay it automatically into a different session.
    for (const cell of optimistic.values()) {cell.status = 'rejected'; cell.detail = t('Session changed. Review your draft and send it again.');}
    consumedItems.clear();
  }
  const insertDraft = snapshot && state.draftRevision > snapshot.draftRevision;
  snapshot = state;
  workbench.update(state);
  overview.update(state);
  if (initial) {
    const saved = vscode.getState(); composer.value = saved?.draft ?? state.draftText;
    for (const entry of saved?.outbox ?? []) {
      localSends.set(entry.input.id, {...entry.input, uploads: []});
      optimistic.set(entry.input.id, {...entry.cell, status: 'rejected', detail: t('The view reopened before delivery was confirmed. Review this message before retrying.')});
    }
    initial = false;
    workbench.input();
  }
  else if (insertDraft) {composer.value = state.draftText; persist(); composer.focus();}
  resizeComposer();
  element('environment').textContent = `${state.environment} · ${state.workspace.split(/[\\/]/).at(-1) ?? ''}`;
  element('environment').title = `${state.environment} · ${state.workspace}`;
  element('model').textContent = state.model || t('Select model');
  const mode = state.mode ? errorText(state.mode) : t('Mode');
  const modeLabel = document.createElement('span'); modeLabel.textContent = mode;
  element('mode').replaceChildren(icon('mode'), modeLabel);
  element('mode').title = `${t('Mode')} · ${mode}`;
  element('mode').setAttribute('aria-label', element('mode').title);
  element('activity').textContent = state.running ? t('Running') : state.phase === 'ready' ? t('Ready') : '';
  element<HTMLButtonElement>('stop').disabled = !state.running;
  const known = new Set(state.cells.map(cell => cell.id)); for (const id of known) {optimistic.delete(id); localSends.delete(id);}
  for (const id of consumedItems) if (!state.draftItems.some(item => item.id === id)) consumedItems.delete(id);
  const cells = [...state.cells, ...optimistic.values()];
  const visible = new Set(cells.map(cell => cell.id)); for (const [id, node] of nodes) if (!visible.has(id)) {node.remove(); nodes.delete(id);}
  for (const cell of cells) cellNode(cell);
  element('empty').hidden = cells.length > 0;
  if (follow) {transcript.scrollTop = transcript.scrollHeight; element('new-output').hidden = true;} else element('new-output').hidden = false;
  const setup = element('setup'); setup.hidden = state.phase === 'ready';
  const phaseTitles = {idle: t('Connect your workspace'), starting: t('Connecting…'), installing: t('Installing…'), stopping: t('Saving and stopping…'), ready: t('Ready'), failed: state.error?.kind === 'missing' ? t('Core not found') : state.error?.kind === 'incompatible' ? t('Core update required') : t('Could not start the core')};
  element('setup-title').textContent = phaseTitles[state.phase];
  element('setup-message').textContent = state.error ? errorText(state.error.message) : t('Start the core on {0} to begin.', state.environment);
  setup.querySelectorAll<HTMLButtonElement>('button').forEach(button => {button.disabled = ['starting', 'stopping', 'installing'].includes(state.phase) && button.dataset.command !== 'logs';});
  const needsInstall = state.phase === 'failed' && ['missing', 'incompatible'].includes(state.error?.kind ?? '');
  setup.querySelector<HTMLButtonElement>('[data-command="start"]')!.classList.toggle('primary', !needsInstall);
  setup.querySelector<HTMLButtonElement>('[data-command="start"]')!.textContent = state.phase === 'failed' ? t('Retry') : t('Start core');
  setup.querySelector<HTMLButtonElement>('[data-command="install"]')!.classList.toggle('primary', needsInstall);
  setup.querySelector<HTMLButtonElement>('[data-command="install"]')!.textContent = state.error?.kind === 'incompatible' ? t('Update core') : t('Install compatible core');
  attention.update(state);
  if (follow) transcript.scrollTop = transcript.scrollHeight;
  if (state.notice) notice(state.notice);
  renderAttachments();
  persist();
}
function renderAttachments(): void {
  const container = element('attachments'); container.replaceChildren();
  for (const item of snapshot?.draftItems ?? []) {
    if (consumedItems.has(item.id)) continue;
    const node = document.createElement('div'); node.className = 'attachment'; const name = document.createElement('span'); name.textContent = item.name;
    node.append(name, button('×', () => void request('remove', {id: item.id}).catch(notice), t('Remove'))); container.append(node);
  }
  for (const upload of uploads.values()) {
    if (upload.submission) continue;
    const node = document.createElement('div'); node.className = `attachment${upload.error ? ' failed' : ''}`; const name = document.createElement('span'); name.textContent = `${upload.file.name} · ${upload.error ? t('Upload failed') : t('Uploading {0}%', upload.progress)}`; node.title = upload.error ?? '';
    node.append(name, button('×', () => {upload.cancelled = true; uploads.delete(upload.id); if (upload.backendId) void request('upload.cancel', {id: upload.backendId}).catch(notice); renderAttachments();}, t('Cancel')));
    if (upload.error) node.append(button(t('Retry'), () => {upload.error = undefined; queueUpload(upload); renderAttachments();}));
    container.append(node);
  }
  element<HTMLButtonElement>('send').disabled = snapshot?.phase !== 'ready';
  element('upload-hint').textContent = [...uploads.values()].some(upload => !upload.submission) ? t('Attachments upload automatically when you send') : t('Add instructions while a task is running');
}
async function send(): Promise<void> {
  if (workbench.consumeSend()) return;
  if (!snapshot || snapshot.phase !== 'ready') {notice(t('Start the workspace core first.')); return;}
  const text = composer.value; const availableItems = snapshot.draftItems.filter(item => !consumedItems.has(item.id)); const items = availableItems.map(item => item.id);
  const attached = [...uploads.values()].filter(upload => !upload.submission);
  if (!text.trim() && !items.length && !attached.length) return;
  const id = crypto.randomUUID(); const generation = snapshot.generation;
  composer.value = ''; saveDraft();
  notice('');
  optimistic.set(id, {id, role: 'user', text: [text, ...availableItems.map(item => `[${item.name}]`), ...attached.map(upload => `[${upload.file.name}]`)].filter(Boolean).join('\n'), status: attached.length ? 'uploading' : 'sending'});
  attached.forEach(upload => {upload.submission = id;});
  localSends.set(id, {id, text, items, generation, hostId: snapshot.hostId, uploads: attached}); items.forEach(id => consumedItems.add(id));
  render(snapshot);
  await retrySend(id);
}
async function retrySend(id: string): Promise<void> {
  const input = localSends.get(id);
  if (!input) {await request('retry', {id}).catch(notice); return;}
  if (input.busy) return;
  if (input.needsFiles) {restoreSend(id); notice(t('Message restored. Check the attachments before sending again.')); return;}
  if (input.hostId !== snapshot?.hostId || input.generation !== snapshot?.generation) {
    restoreSend(id); return;
  }
  input.busy = true;
  try {
    for (const upload of input.uploads) if (upload.error) {upload.error = undefined; upload.cancelled = false; queueUpload(upload);}
    if (input.uploads.length) {
      await uploadQueue;
      if (input.cancelled) return;
      if (input.hostId !== snapshot?.hostId || input.generation !== snapshot?.generation) throw new Error(t('Session changed. Review your draft and send it again.'));
      for (const upload of input.uploads) {
        if (!upload.attachmentId || upload.error) throw new Error(upload.error ?? t('Attachment upload is incomplete.'));
        if (!input.items.includes(upload.attachmentId)) input.items.push(upload.attachmentId);
        consumedItems.add(upload.attachmentId);
      }
    }
    const cell = optimistic.get(id); if (cell) {cell.status = 'sending'; cell.detail = ''; render(snapshot!);}
    await request('send', {id, text: input.text, items: input.items, generation: input.generation, hostId: input.hostId}); notice('');
  }
  catch (error) {const cell = optimistic.get(id); if (cell) {cell.status = 'rejected'; cell.detail = errorText(error);} notice(error); if (snapshot) render(snapshot);}
  finally {input.busy = false;}
}
function restoreSend(id: string): void {
  const input = localSends.get(id); if (!input) return;
  input.cancelled = true;
  input.items.forEach(id => consumedItems.delete(id));
  input.uploads.forEach(upload => {
    upload.submission = undefined; if (upload.attachmentId) consumedItems.delete(upload.attachmentId);
    if (input.hostId !== snapshot?.hostId || input.generation !== snapshot?.generation) {upload.attachmentId = undefined; upload.backendId = undefined; upload.error = undefined; upload.cancelled = false; uploads.set(upload.id, upload); queueUpload(upload);}
  });
  composer.value = [composer.value, input.text].filter(Boolean).join('\n'); saveDraft(); composer.focus();
  optimistic.delete(id); localSends.delete(id); if (snapshot) render(snapshot);
}
function queueUpload(upload: LocalUpload): void {
  uploadQueue = uploadQueue.then(async () => {
    if (upload.cancelled) return;
    try {
      const image = upload.file.type.startsWith('image/');
      if (image && !['image/png', 'image/jpeg', 'image/webp'].includes(upload.file.type)) throw new Error(t('Only PNG, JPEG and WebP images are supported.'));
      if (upload.file.size > 64 * 1024 * 1024) throw new Error(t('Attachment exceeds 64 MiB.'));
      const started = await request('upload.begin', {name: upload.file.name || 'clipboard.png', size: upload.file.size, image});
      upload.backendId = started.id;
      for (let offset = 0; offset < upload.file.size;) {
        if (upload.cancelled) throw new Error(t('Attachment upload cancelled.'));
        const bytes = new Uint8Array(await upload.file.slice(offset, offset + started.chunk).arrayBuffer());
        let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
        offset = await request('upload.append', {id: started.id, offset, data: btoa(binary)});
        upload.progress = Math.round(offset / upload.file.size * 100); renderAttachments();
        const cell = upload.submission ? optimistic.get(upload.submission) : undefined;
        if (cell) {cell.detail = `${upload.file.name} · ${t('Uploading {0}%', upload.progress)}`; cellNode(cell);}
      }
      if (upload.cancelled) throw new Error(t('Attachment upload cancelled.'));
      upload.attachmentId = await request('upload.complete', {id: started.id});
      if (upload.submission && upload.attachmentId) consumedItems.add(upload.attachmentId);
      uploads.delete(upload.id);
    } catch (error) {upload.error = errorText(error); if (!upload.cancelled) notice(error);}
    finally {if (upload.backendId) await request('upload.cancel', {id: upload.backendId}).catch(notice); renderAttachments();}
  });
}
function addFiles(files: File[]): void {for (const file of files) {const upload: LocalUpload = {id: crypto.randomUUID(), file, progress: 0, cancelled: false}; uploads.set(upload.id, upload); queueUpload(upload);} renderAttachments();}
window.addEventListener('message', event => {
  const message = event.data;
  if (message?.kind === 'response') {const entry = pending.get(message.id); if (!entry) return; pending.delete(message.id); clearTimeout(entry.timer); message.error ? entry.reject(new Error(message.error)) : entry.resolve(message.result);}
  else if (message?.kind === 'snapshot') render(message.snapshot);
  else if (message?.kind === 'error') notice(message.error);
  else if (message?.kind === 'commands') workbench.show();
});
composer.addEventListener('input', () => {saveDraft(); workbench.input();});
composer.addEventListener('keydown', event => {if (workbench.key(event)) return; if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); void send();}});
composer.addEventListener('paste', event => {const files = [...(event.clipboardData?.files ?? [])]; if (files.length) {event.preventDefault(); addFiles(files);}});
element('send').addEventListener('click', () => void send());
element('new-output').addEventListener('click', () => {transcript.scrollTop = transcript.scrollHeight; element('new-output').hidden = true;});
element('attach').addEventListener('click', () => element<HTMLInputElement>('files').click());
element('commands').addEventListener('click', () => workbench.show());
element('attention-jump').addEventListener('click', () => element('reviews').scrollIntoView({block: 'start', behavior: 'smooth'}));
element<HTMLInputElement>('files').addEventListener('change', event => {const input = event.target as HTMLInputElement; addFiles([...input.files ?? []]); input.value = '';});
element('drop-zone').addEventListener('dragover', event => {event.preventDefault(); element('drop-zone').classList.add('drop-active');});
element('drop-zone').addEventListener('dragleave', () => element('drop-zone').classList.remove('drop-active'));
element('drop-zone').addEventListener('drop', event => {
  event.preventDefault(); element('drop-zone').classList.remove('drop-active');
  const files = [...event.dataTransfer?.files ?? []]; if (files.length) addFiles(files);
  else for (const uri of (event.dataTransfer?.getData('text/uri-list') ?? '').split(/\r?\n/).filter(line => line && !line.startsWith('#'))) void request('addUri', {uri}).catch(notice);
});
document.querySelectorAll<HTMLButtonElement>('[data-command]').forEach(button => button.addEventListener('click', () => void request(button.dataset.command!).catch(notice)));
document.querySelectorAll<HTMLButtonElement>('[data-action]').forEach(button => button.addEventListener('click', () => void request('command.open', {actionId: button.dataset.action!}).catch(notice)));
void request('ready').then(render).catch(notice);
