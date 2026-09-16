import {EventEmitter} from 'node:events';
import {isDeepStrictEqual} from 'node:util';
import type {Key} from 'ink';
import {RuntimeClient} from '../protocol/client.js';
import {cancellation, record, typeOf, type Action, type Json, type Panel, type PanelItem, type PendingInteraction, type View} from '../protocol/wire.js';
import {SessionStore} from './session.js';
import {edit, editor, type Editor} from './editor.js';
import {actionLabel, defaults, fieldValue, humanize, menusFromCatalog, type Menu} from './menus.js';
import {InputHistory} from './history.js';
import {HistoryBrowser} from './history-browser.js';
import type {HistoryOperation} from '../protocol/history.js';
import {fields} from '../ui/format.js';
import {ScrollMotion} from './scroll.js';

export interface Item {label: string; description: string; current?: boolean; id?: string | null; select(): void | Promise<void>}
export interface ListScreen {kind: 'list'; title: string; items: Item[]; index: number; filter: Editor; menu?: Menu; panel?: Panel; panelPath?: string[]; output?: string; contentOffset?: number | null; contentHeight?: number; contentEnd?: number}
export interface DocumentScreen {kind: 'document'; title: string; body: string; offset: number; menu?: Menu}
export interface FormScreen {kind: 'form'; title: string; action: Action; values: {[key: string]: Json}; index: number; input: Editor; error: string}
export interface HistoryScreen {kind: 'history'; title: string; browser: HistoryBrowser; menu?: never}
export type Screen = ListScreen | DocumentScreen | FormScreen | HistoryScreen;

export class TuiController extends EventEmitter {
  readonly session = new SessionStore();
  menus: Menu[] = [];
  composer = editor();
  screens: Screen[] = [];
  paletteIndex = 0;
  paletteDismissed = false;
  expanded = false;
  offset: number | null = null;
  totalRows = 0;
  viewportRows = 10;
  columns = 80;
  rows = 24;
  exitConfirm = false;
  closing = false;
  status = '';
  interactionMode: 'review' | 'scope' | 'feedback' = 'review';
  interactionIndex = 0;
  interactionInput = editor();
  interactionOffset = 0;
  private interactionId?: string;
  private revision = 0;
  private updateTimer?: NodeJS.Timeout;
  private viewEpoch = 0;
  private pendingView = Promise.resolve();
  private historyIndex: number | null = null;
  private historyDraft = editor();
  private refreshTimer?: NodeJS.Timeout;
  private scroll = new ScrollMotion(() => {this.revision++; this.flush();});

  constructor(readonly client: RuntimeClient, readonly history = new InputHistory()) {
    super();
    this.session.on('change', this.changed);
    this.session.on('view', (view, wire) => {
      const epoch = this.viewEpoch;
      this.pendingView = this.pendingView.then(() => this.openView(view, wire, epoch)).catch(this.fail);
    });
    client.on('initialized', info => {this.menus = menusFromCatalog(client.catalog); this.session.initialize(info);});
    client.on('state', state => this.session.update(state));
    client.on('event', (event, wire, generation) => {
      this.session.event(event, wire, generation);
      if (event.message && !['RuntimeEventPayload', 'ViewEventPayload', 'InteractionPromptPayload'].includes(typeOf(event.payload) ?? '')) this.status = event.message;
      if (this.screen && typeOf(event.payload) === 'ReasoningNoticePayload') this.document(event.payload.title, event.message);
    });
    client.on('completed', result => {this.session.completed(result); if (result.control === 'exit') void this.finish().catch(this.fail);});
    client.on('command', text => this.session.notice(text));
    client.on('operationFailure', message => this.fail(new Error(message)));
    client.on('failure', error => {this.session.fatal = error.message; this.session.connected = false; this.fail(error);});
    client.on('answered', (item, response) => this.session.reviewed(item.request, response));
    client.on('interactions', () => {
      const active = client.interactions[0];
      if (active?.request.request_id !== this.interactionId) {
        this.interactionId = active?.request.request_id;
        this.interactionMode = 'review'; this.interactionIndex = 0; this.interactionOffset = 0;
        this.interactionInput = editor(active?.kind === 'input_text' ? active.request.initial_value : '');
        if (active?.kind === 'choose_one') this.interactionIndex = Math.max(0, active.request.items.findIndex((item: any) => item.id === active.request.initial_id));
      }
      this.changed();
    });
  }
  snapshot = () => this.revision;
  subscribe = (listener: () => void) => {this.on('change', listener); return () => {this.off('change', listener);};};
  changed = () => {
    this.revision++;
    if (!this.updateTimer) this.updateTimer = setTimeout(this.flush, 16);
  };
  private flush = () => {clearTimeout(this.updateTimer); this.updateTimer = undefined; this.emit('change');};
  fail = (error: Error) => {this.status = error.message; this.session.notice(error.message, 'error');};
  get active(): PendingInteraction | undefined {return this.client.interactions[0];}
  get screen(): Screen | undefined {return this.screens.at(-1);}
  get palette(): Menu[] {
    if (this.active || this.screen || this.paletteDismissed || !this.composer.text.startsWith('/')) return [];
    const query = this.composer.text.trim().toLowerCase();
    return this.menus.filter(menu => menu.name.startsWith(query));
  }
  listItems(screen: ListScreen) {const query = screen.filter.text.toLowerCase(); return screen.items.filter(item => `${item.label} ${item.description}`.toLowerCase().includes(query));}

  startRefresh() {
    let refreshing = false;
    let gitRefreshing = false, nextGitRefresh = 0, lastStateRefresh = -Infinity;
    this.refreshTimer = setInterval(() => {
      if (!this.session.connected || this.closing) return;
      const now = performance.now();
      if (!refreshing && now - lastStateRefresh >= (this.session.state.running || this.active ? 500 : 5000)) {
        refreshing = true;
        lastStateRefresh = now;
        void this.client.refresh().catch(error => {this.session.connected = false; this.session.fatal = error.message; this.fail(error);}).finally(() => {refreshing = false;});
      }
      if (this.client.info.workspace_git && !gitRefreshing && performance.now() >= nextGitRefresh) {
        gitRefreshing = true;
        nextGitRefresh = performance.now() + 5000;
        void this.client.git().then(git => {if (!isDeepStrictEqual(this.session.git, git)) {this.session.git = git; this.changed();}})
          .catch(this.fail).finally(() => {gitRefreshing = false;});
      }
    }, 500);
    this.refreshTimer.unref();
  }
  resize(rows: number, columns: number) {
    if (this.rows === rows && this.columns === columns) return;
    this.scroll.cancel();
    this.rows = rows; this.columns = columns;
    if (this.session.connected && !this.closing) this.client.resize(rows, columns);
    this.changed();
  }
  document(title: string, body: string, menu?: Menu) {this.screens.push({kind: 'document', title, body, offset: 0, menu}); this.changed();}
  showHistory(operation: HistoryOperation = 'read', parameters?: Record<string, any>) {
    if (this.screen?.kind === 'history') this.screen.browser.dispose();
    const browser = new HistoryBrowser(this.client, this.changed, operation, parameters);
    this.screens.push({kind: 'history', title: 'Session history', browser});
    void browser.load(); this.changed();
  }
  private list(title: string, items: Item[], menu?: Menu, panel?: Panel): ListScreen {return {kind: 'list', title, items, index: Math.max(0, items.findIndex(item => item.current)), filter: editor(), menu, panel};}
  private actionItems(menu: Menu): Item[] {return menu.actions.map(action => ({label: actionLabel(action), description: action.description.match(/^(?:\[[^\]]+\]\s*)+/)?.[0] ?? '', select: () => this.chooseAction(action)}));}
  async openMenu(menu: Menu) {
    this.viewEpoch++;
    this.screens = [this.list(menu.title, this.actionItems(menu), menu)];
    this.paletteDismissed = true; this.changed();
    const preview = menu.actions.find(action => action.preview && action.parameters.every(parameter => !parameter.required));
    if (preview) await this.client.submitAction(preview.action_id, defaults(preview));
  }
  private async openView(view: View, wire: Json | undefined, epoch: number) {
    if (epoch !== this.viewEpoch || !wire || this.screen?.kind === 'form') return;
    const presentation = await this.client.panel(wire);
    if (epoch !== this.viewEpoch) return;
    const parent = this.screens.find(screen => screen.kind !== 'form' && screen.menu);
    const menu = parent && parent.kind !== 'form' ? parent.menu : undefined;
    if (view.action === 'refresh' || !view.focus) {
      if (presentation?.refresh !== 'update') return;
      const index = this.screens.findIndex(screen => screen.kind === 'list' && screen.panel?.view_type === presentation.definition.view_type);
      if (index < 0) return;
      const panels: {panel: Panel; path: string[]}[] = [];
      const collect = (panel: Panel, path: string[]) => {panels.push({panel, path}); for (const [id, child] of panel.children) collect(child, [...path, id]);};
      collect(presentation.definition, []);
      for (let i = index; i < this.screens.length; i++) {
        const previous = this.screens[i];
        if (previous.kind !== 'list' || !previous.panel) continue;
        const matches = panels.filter(item => item.panel.view_type === previous.panel!.view_type);
        const match = matches.find(item => isDeepStrictEqual(item.path, previous.panelPath)) ?? (matches.length === 1 ? matches[0] : undefined);
        if (!match) {this.screens.length = i; break;}
        const {panel, path} = match;
        const replacement = this.panelScreen(panel, view, previous.menu, path);
        replacement.filter = previous.filter;
        replacement.output = panel.output ?? previous.output;
        replacement.contentOffset = previous.contentOffset === previous.contentEnd ? null : previous.contentOffset;
        const selected = this.listItems(previous)[previous.index];
        replacement.index = Math.max(0, this.listItems(replacement).findIndex(item => (item.id ?? item.label) === (selected?.id ?? selected?.label)));
        this.screens[i] = replacement;
      }
      this.changed(); return;
    }
    this.screens = presentation ? [this.panelScreen(presentation.definition, view, menu)] : [{kind: 'document', title: view.title, body: fields(view.view_model), offset: 0, menu}];
    this.changed();
  }
  private panelScreen(panel: Panel, view: View, menu?: Menu, path: string[] = []): ListScreen {
    const items: Item[] = panel.items.map(item => ({label: item.label, description: item.description, current: item.current, id: item.id, select: () => this.selectPanel(panel, item, view, menu)}));
    if (panel.show_auxiliary_actions !== false) {
      items.push({label: 'View all details', description: 'Inspect every field in this view', select: () => this.document(view.title, fields(view.view_model), menu)});
      if (menu) items.push({label: 'More actions…', description: 'All operations and parameter forms', select: () => {this.screens.push(this.list(menu.title, this.actionItems(menu), menu)); this.changed();}});
    }
    return {...this.list(panel.title, items, menu, panel), panelPath: path, output: panel.output ?? undefined, contentOffset: null};
  }
  private async selectPanel(panel: Panel, item: PanelItem, view: View, menu?: Menu) {
    const child = panel.children.find(([id]) => id === (item.id ?? item.label))?.[1];
    if (child) {
      const path = this.screen?.kind === 'list' ? this.screen.panelPath ?? [] : [];
      this.screens.push(this.panelScreen(child, view, menu, [...path, item.id ?? item.label])); this.changed();
      if (child.on_open) await this.client.submitAction(child.on_open.action_id, child.on_open.command);
      return;
    }
    if (!item.action) return;
    if (!panel.keep_open_on_submit) {
      if (panel.return_to_parent_on_submit) this.screens.pop();
      else {this.screens = []; this.viewEpoch++;}
    }
    await this.client.submitAction(item.action.action_id, item.action.command);
    this.changed();
  }
  private async chooseAction(action: Action) {
    if (action.parameters.length) {
      const parameter = action.parameters[0];
      this.screens.push({kind: 'form', title: actionLabel(action), action, values: {}, index: 0, input: editor(parameter.default == null ? '' : String(parameter.default)), error: ''});
      this.changed(); return;
    }
    if (!action.preview) {this.screens = []; this.viewEpoch++;}
    await this.client.submitAction(action.action_id);
    this.changed();
  }
  private async submitForm(screen: FormScreen) {
    const parameter = screen.action.parameters[screen.index];
    try {screen.values[parameter.name] = fieldValue(parameter, screen.input.text);}
    catch (error) {screen.error = (error as Error).message; this.changed(); return;}
    screen.error = '';
    if (++screen.index < screen.action.parameters.length) {
      const next = screen.action.parameters[screen.index];
      screen.input = editor(next.default == null ? '' : String(next.default)); this.changed(); return;
    }
    this.screens.pop();
    await this.client.submitAction(screen.action.action_id, screen.values);
    this.changed();
  }
  private answer(response: Json) {if (this.active) this.client.answer(this.active.request.request_id, response);}
  private interactionKey(input: string, key: Partial<Key>) {
    const {kind, request} = this.active!;
    if (key.escape) {
      if (this.interactionMode !== 'review') {this.interactionMode = 'review'; this.interactionInput = editor();}
      else if (kind !== 'choose_one' || request.allow_cancel) this.answer(cancellation(kind));
      return;
    }
    if (kind === 'input_text' || this.interactionMode === 'feedback') {
      if (key.return && !key.shift && !key.meta) {
        const value = this.interactionInput.text;
        if (this.interactionMode === 'feedback') {
          if (value.trim()) this.answer(record('ReviewResponse', {approved: false, action: 'deny', reason: value}));
          else this.interactionMode = 'review';
        } else if (value || request.allow_empty) this.answer(record('InputTextResponse', {value}));
        else this.status = 'Enter a value, or press Esc to cancel.';
      } else this.interactionInput = edit(this.interactionInput, key.return ? '\n' : input, key.return ? {} : key);
      return;
    }
    const choices = kind === 'choose_one' ? request.items : this.interactionMode === 'scope' ? request.grant_options : [];
    if (choices.length) {
      if (key.upArrow || key.downArrow) this.interactionIndex = (this.interactionIndex + (key.upArrow ? -1 : 1) + choices.length) % choices.length;
      const numeric = /^[1-9]$/.test(input) ? Number(input) - 1 : -1;
      if (key.return || numeric >= 0 && numeric < choices.length) {
        const chosen = choices[numeric >= 0 ? numeric : this.interactionIndex];
        this.answer(kind === 'choose_one' ? record('ChooseOneResponse', {selected_id: chosen.id}) : record('ReviewResponse', {approved: true, action: 'allow_session', selected_id: chosen.id}));
      }
      return;
    }
    if (kind === 'choose_one') return;
    if (key.upArrow || key.downArrow) {this.interactionOffset = Math.max(0, this.interactionOffset + (key.upArrow ? -3 : 3)); return;}
    if (kind === 'review' && input === 's' && request.grant_options.length) {this.interactionMode = 'scope'; this.interactionIndex = 0; return;}
    if (kind === 'review' && input === 'f') {this.interactionMode = 'feedback'; this.interactionInput = editor(); return;}
    if (['y', 'n', '1', '2'].includes(input) || key.return) {
      const yes = key.return || input === 'y' || input === '1';
      this.answer(kind === 'confirm' ? record('ConfirmResponse', {confirmed: yes}) : record('ReviewResponse', {approved: yes, action: yes ? 'allow_once' : 'deny'}));
    }
  }
  paste(text: string) {
    if (this.active && (this.active.kind === 'input_text' || this.interactionMode === 'feedback')) this.interactionInput = edit(this.interactionInput, text, {});
    else if (this.screen?.kind === 'form') this.screen.input = edit(this.screen.input, text, {});
    else if (this.screen?.kind === 'history' && this.screen.browser.search) this.screen.browser.search = edit(this.screen.browser.search, text, {});
    else if (!this.active && !this.screen) {this.composer = edit(this.composer, text, {}); this.paletteDismissed = false;}
    this.changed();
  }
  async key(input: string, key: Partial<Key> = {}) {
    if (this.closing) return;
    if (!(key.upArrow || key.downArrow || key.pageUp || key.pageDown)) this.scroll.cancel();
    if (!this.active && this.screen?.kind === 'history' && !(key.ctrl && ['c', 'd', 'o', 'r', 'g'].includes(input))) {
      const browser = this.screen.browser;
      if (key.escape) {
        if (browser.search) browser.search = null;
        else {browser.dispose(); this.screens.pop();}
      } else if (browser.search) {
        if (key.return) {
          const pattern = browser.search.text.trim();
          browser.search = null;
          if (pattern) this.showHistory('search', {pattern});
        } else browser.search = edit(browser.search, input, key);
      } else if (input === '/') browser.beginSearch();
      else if (input === 'n' || key.rightArrow) void browser.next();
      else if (input === 'p' || key.leftArrow) void browser.previous();
      else if (input === 'r') void browser.load();
      else if (input === 'm' && !browser.detailed) this.showHistory('read', {reverse: true, messages_only: browser.parameters.messages_only === false});
      else if (key.return && browser.current && !browser.detailed) this.showHistory('read', {event_id: browser.current.event_id});
      else if (input === 'a' && browser.current?.artifact_refs.length) {
        this.screens.push(this.list('History artifacts', browser.current.artifact_refs.map(artifact_ref => ({label: artifact_ref, description: 'Read archived content', select: () => this.showHistory('artifact', {artifact_ref, session_id: browser.page!.session_id})}))));
      } else if (key.upArrow || key.downArrow) {
        if (browser.detailed) {this.scrollBy(key.upArrow ? -1 : 1); return;}
        else browser.index = Math.max(0, Math.min((browser.page?.records.length ?? 1) - 1, browser.index + (key.upArrow ? -1 : 1)));
      } else if (key.pageUp || key.pageDown) {
        const delta = (key.pageUp ? -1 : 1) * this.viewportRows;
        if (browser.detailed) {this.scrollBy(delta); return;}
        else browser.index = Math.max(0, Math.min((browser.page?.records.length ?? 1) - 1, browser.index + Math.trunc(delta / 2)));
      }
      else if (key.home) browser.offset = 0;
      else if (key.end) browser.offset = Number.MAX_SAFE_INTEGER;
      this.changed(); return;
    }
    if (key.ctrl && input === 'c') {
      if (this.active) this.answer(cancellation(this.active.kind));
      else if (this.screen) {for (const screen of this.screens) if (screen.kind === 'history') screen.browser.dispose(); this.screens = []; this.viewEpoch++;}
      else if (this.composer.text) {this.composer = editor(); this.exitConfirm = false;}
      else if (this.session.state.stopping) await this.finish();
      else if (this.session.state.running && !this.session.fatal) {
        const result = await this.client.interrupt();
        this.status = result.outcome === 'promoted' ? 'Applying queued steering; Ctrl+C again stops the turn.' : '';
      } else if (this.exitConfirm) await this.finish();
      else this.exitConfirm = true;
      this.changed(); return;
    }
    if (key.ctrl && input === 'd' && !this.composer.text && !this.active) {await this.finish(); return;}
    if (input === '[12~' || input === '\x1bOQ' || key.ctrl && input === 'o') {this.showSession(); return;}
    if (input === '[14~' || input === '\x1bOS' || key.ctrl && input === 'r') {this.toggleDetails(); return;}
    if (input === 'OP' || input === '\x1bOP' || key.ctrl && input === 'g') {this.showHelp(); return;}
    if (key.pageUp || key.pageDown) {
      const delta = (key.pageUp ? -1 : 1) * Math.max(1, this.viewportRows - 1);
      if (!this.active && this.screen?.kind === 'list') {
        if (this.screen.panel?.body) this.screen.contentOffset = Math.max(0, (this.screen.contentOffset ?? 0) + (key.pageUp ? -1 : 1) * Math.max(1, (this.screen.contentHeight ?? 1) - 1));
        else this.screen.index = Math.max(0, Math.min(this.listItems(this.screen).length - 1, this.screen.index + delta));
      }
      else {this.scrollBy(delta); return;}
      this.changed(); return;
    }
    if (this.active) {this.interactionKey(input, key); this.changed(); return;}
    if (key.escape) {
      this.exitConfirm = false;
      if (this.screen) {this.screens.pop(); this.viewEpoch++;}
      else this.paletteDismissed = true;
      this.changed(); return;
    }
    if (this.screen) {
      const screen = this.screen;
      if (screen.kind === 'document' && screen.title === 'Session details' && input === 'h' && this.client.info.history_query) {this.showHistory(); return;}
      if (screen.kind === 'list') {
        const items = this.listItems(screen);
        if (key.upArrow || key.downArrow) screen.index = items.length ? (screen.index + (key.upArrow ? -1 : 1) + items.length) % items.length : 0;
        else if (key.return) await items[screen.index]?.select();
        else if (screen.panel?.body && (key.home || key.end)) screen.contentOffset = key.home ? 0 : null;
        else if (screen.panel?.filterable !== false) {screen.filter = edit(screen.filter, input, key); screen.index = 0;}
      } else if (screen.kind === 'document') {
        if (key.tab && screen.menu) this.screens.push(this.list(screen.menu.title, this.actionItems(screen.menu), screen.menu));
        else if (key.upArrow || key.downArrow) {this.scrollBy(key.upArrow ? -1 : 1); return;}
        else if (key.home) screen.offset = 0;
        else if (key.end) screen.offset = Number.MAX_SAFE_INTEGER;
      } else if (screen.kind === 'history') return;
      else if (key.return && !key.shift && !key.meta) await this.submitForm(screen);
      else if (key.upArrow && screen.index > 0) {screen.index--; screen.input = editor(String(screen.values[screen.action.parameters[screen.index].name] ?? ''));}
      else if ((key.tab || key.leftArrow || key.rightArrow || input === ' ') && screen.action.parameters[screen.index].kind === 'boolean') {
        const choices = screen.action.parameters[screen.index].nullable ? ['auto', 'true', 'false'] : ['true', 'false'];
        screen.input = editor(choices[(choices.indexOf(screen.input.text) + 1) % choices.length]);
      } else screen.input = edit(screen.input, key.return ? '\n' : input, key.return ? {} : key);
      this.changed(); return;
    }
    if (key.ctrl && input === 'p') {this.screens.push(this.list('Commands', this.menus.map(menu => ({label: menu.name, description: menu.title, select: () => this.openMenu(menu)})))); this.changed(); return;}
    if (this.palette.length && (key.upArrow || key.downArrow || key.tab || key.return)) {
      const menus = this.palette;
      if (key.upArrow || key.downArrow) this.paletteIndex = (this.paletteIndex + (key.upArrow ? -1 : 1) + menus.length) % menus.length;
      else if (key.tab) this.composer = editor(menus[this.paletteIndex % menus.length].name);
      else {const menu = menus[this.paletteIndex % menus.length]; this.composer = editor(); await this.openMenu(menu);}
      this.changed(); return;
    }
    if (key.return && !key.shift && !key.meta) {
      const text = this.composer.text.trim();
      if (!text) return;
      if (text.startsWith('/')) {
        const menu = this.menus.find(menu => menu.name === text.split(/\s/)[0]);
        if (menu) {this.composer = editor(); await this.openMenu(menu);}
        else this.status = 'Choose a command from / or Ctrl+P.';
      } else {
        const draft = this.composer;
        const admission = await this.client.submit(text);
        if (admission.status !== 'rejected') {if (this.composer === draft) this.composer = editor(); this.historyIndex = null; this.offset = null; await this.history.add(text);}
        else this.status = 'The backend is stopping. Your draft is still here.';
      }
    } else if (key.home && !this.composer.text) this.offset = 0;
    else if (key.end && !this.composer.text) this.offset = null;
    else if ((key.upArrow || key.downArrow) && !key.meta && !this.composer.text && this.historyIndex === null) {this.scrollBy(key.upArrow ? -3 : 3); return;}
    else if (key.upArrow || key.downArrow) {
      if (this.historyIndex === null) {this.historyDraft = this.composer; this.historyIndex = this.history.entries.length;}
      this.historyIndex = Math.max(0, Math.min(this.history.entries.length, this.historyIndex + (key.upArrow ? -1 : 1)));
      this.composer = this.historyIndex === this.history.entries.length ? this.historyDraft : editor(this.history.entries[this.historyIndex]);
    } else {this.composer = edit(this.composer, key.return ? '\n' : input, key.return ? {} : key); this.paletteDismissed = false; this.paletteIndex = 0; this.exitConfirm = false;}
    this.changed();
  }
  toggleDetails() {
    this.scroll.cancel();
    this.expanded = !this.expanded;
    this.status = this.expanded
      ? 'Full details shown · F4 collapse output + reasoning'
      : 'Compact view · F4 show tool output + reasoning';
    this.changed();
  }
  showSession() {
    if (this.screen?.title === 'Session details') {this.screens.pop(); this.changed(); return;}
    this.document('Session details', fields({session: this.session.state, git: this.session.git, plan: this.session.plan, progress: this.session.progress, agents: [...this.session.jobs.values()], processes: [...this.session.processes.values()], diagnostics: [...this.session.diagnostics.values()], operations: [...this.session.operations.values()], startup: this.session.startup}));
  }
  showHelp() {this.document('Keyboard help', 'Enter        Send / select\nShift+Enter  New line (Alt+Enter also works)\n/ or Ctrl+P  Open command menus\nEsc          Back / cancel interaction\nCtrl+C       Cancel interaction → close menu → clear draft → interrupt → confirm exit\nCtrl+D       Exit with an empty draft\nUp / Down    Scroll when input is empty; otherwise input history\nAlt+Up/Down  Input history, including with an empty draft\nPgUp / PgDn  Scroll the focused content\nHome / End   Transcript start / follow tail (empty composer)\nF1 / Ctrl+G  Keyboard help\nF2 / Ctrl+O  Session, plan, jobs and startup details\nF4 / Ctrl+R  Toggle full transcript details (all records)\nCtrl+A/E     Start / end of input\nCtrl+U/K/W   Delete before / after / previous word\n\nF4 shows:\n  Tool arguments and full received output, diffs, diagnostics and archive details.\n  Reasoning returned by the model.\nIt applies to all retained records in this conversation.\nPress F4 again to restore previews; PgUp/PgDn reads earlier content.\n\nApproval: 1/y/Enter approve once · 2/n deny · s session scope · f feedback\nSecret input is masked and never written to input history.');}
  async finish() {
    if (this.closing) return;
    this.scroll.cancel();
    this.closing = true; clearInterval(this.refreshTimer); this.changed();
    try {const saved = await this.client.shutdown(); this.emit('exit', saved);}
    catch (error) {this.emit('exit', null, error);}
  }
  dispose() {this.scroll.cancel(); clearInterval(this.refreshTimer); clearTimeout(this.updateTimer); this.removeAllListeners();}

  private scrollBy(delta: number) {
    const active = this.active, screen = this.screen, cells = this.session.cells;
    if (active) {
      this.scroll.move(active, delta, () => this.interactionOffset, row => {this.interactionOffset = row;}, () => this.active === active);
    } else if (screen?.kind === 'document' || screen?.kind === 'history' && screen.browser.detailed) {
      const position = screen.kind === 'document' ? screen : screen.browser;
      this.scroll.move(position, delta, () => position.offset, row => {position.offset = row;}, () => !this.active && this.screen === screen);
    } else {
      const bottom = Math.max(0, this.totalRows - this.viewportRows);
      this.scroll.move(cells, delta, () => this.offset ?? bottom, row => {this.offset = row;}, () => !this.active && this.screen === screen && this.session.cells === cells, bottom);
    }
  }
}
