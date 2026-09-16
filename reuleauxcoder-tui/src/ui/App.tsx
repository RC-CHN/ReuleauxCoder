import React, {memo, useEffect, useMemo, useState, useSyncExternalStore} from 'react';
import {Box, Text, useApp, useCursor, useInput, usePaste, useStdout} from 'ink';
import type {TuiController} from '../state/controller.js';
import {safe} from './format.js';
import {inputLayout, TranscriptLayout} from './viewport.js';
import {hintRows, panelRows} from './panels.js';
import {useAlternateScroll, useTerminalKeys} from './terminal.js';
import {between, fit, rail, frameEdge, frameRow, keyHint, paint} from './theme.js';
import {activityFor, ActivityLine} from './activity.js';
import {queuedRows} from './queued.js';
import {sidebarRows, workbenchLayout} from './sidebar.js';
import {consoleChrome, useLogoCollapse} from './chrome.js';
import {ComposerEdge} from './composer-edge.js';
import {Reveal} from './reveal.js';
import {TextLayout} from './text-layout.js';

const Rows = memo(function Rows({rows, height, width}: {rows: string[]; height: number; width: number}) {
  const text = Array.from({length: height}, (_, index) => paint.surface(fit(rows[index] || '', width))).join('\n');
  return <Box flexDirection="column" height={height} flexShrink={0}><Text wrap="truncate">{text}</Text></Box>;
}, (before, after) => before.width === after.width && before.height === after.height
  && before.rows.length === after.rows.length && before.rows.every((row, index) => row === after.rows[index]));

function ProcessSidebar({controller, width, height}: {controller: TuiController; width: number; height: number}) {
  const [now, setNow] = useState(Date.now);
  const running = [...controller.session.processes.values()].some(process => process.state === 'running');
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return <Rows rows={sidebarRows(controller, width, height, now)} height={height} width={width}/>;
}

export function App({controller: c, alternateScreen = false}: {controller: TuiController; alternateScreen?: boolean}) {
  useSyncExternalStore(c.subscribe, c.snapshot, c.snapshot);
  const {stdout} = useStdout();
  const {exit} = useApp();
  const {setCursorPosition} = useCursor();
  const layout = useMemo(() => new TranscriptLayout(), []);
  const panelLayout = useMemo(() => new TextLayout(), [c.active, c.screen]);
  const hiddenLogoRows = useLogoCollapse();
  useEffect(() => {
    const resize = () => c.resize(stdout.rows || 24, stdout.columns || 80);
    const quit = (saved: string | null, error?: Error) => exit(error ?? saved);
    resize(); stdout.on('resize', resize); c.on('exit', quit);
    return () => {stdout.off('resize', resize); c.off('exit', quit);};
  }, [c, stdout, exit]);
  useInput((input, key) => {void c.key(input, key).catch(c.fail);});
  usePaste(text => c.paste(text));
  useTerminalKeys(c);
  useAlternateScroll(alternateScreen);

  const dimensions = workbenchLayout(c.columns, c.rows);
  const width = dimensions.main;
  const height = Math.max(6, c.rows - 1);
  const liveActivity = activityFor(c);
  const chrome = consoleChrome(c, dimensions.width, c.session.fatal ? 'Disconnected' : liveActivity?.label ?? 'Ready', hiddenLogoRows);
  const footerHeight = chrome.status ? 2 : 1;
  const composerWidth = Math.max(1, dimensions.width - 6);
  const focused = !c.active && !c.screen;
  const composerInput = inputLayout(c.composer, composerWidth, 4, focused);
  const composerHeight = Math.max(1, composerInput.rows.length);
  const bodyBudget = height - chrome.header.length - footerHeight - composerHeight - 2;
  const contentHeight = Math.max(1, bodyBudget - (liveActivity ? 1 : 0));
  const panelWidth = Math.max(1, width - 2);
  const panelHintBudget = Math.min(3, Math.ceil(90 / panelWidth));
  const hasPanel = Boolean(c.active || c.screen || c.palette.length);
  const queue = queuedRows(c.session.state, dimensions.width, Math.max(0, Math.min(5, contentHeight - (hasPanel ? 5 : 2))));
  const bodyHeight = bodyBudget - queue.length;
  const available = contentHeight - queue.length;
  const listLength = c.screen?.kind === 'list' ? c.listItems(c.screen).length : c.palette.length;
  const desiredPanelHeight = c.screen?.kind === 'history' ? available : c.active || c.screen?.kind === 'document' || c.screen?.kind === 'form' ? 18 : Math.min(18, listLength * (panelWidth >= 45 ? 2 : 1) + (c.screen ? 1 : 0));
  const panelCapacity = hasPanel ? Math.max(1, Math.min(desiredPanelHeight, available - (c.active ? 1 : 4) - panelHintBudget)) : 0;
  const panel = hasPanel ? panelRows(c, panelWidth, panelCapacity, panelLayout) : null;
  const panelHeight = panel ? Math.max(1, Math.min(panelCapacity, panel.rows.length)) : 0;
  const panelHints = panel ? hintRows(panel.hint, panelWidth).slice(0, panelHintBudget) : [];
  const transcriptHeight = Math.max(0, available - (panel ? panelHeight + 1 + panelHints.length : 0));
  const transcript = useMemo(() => layout.render(c.session.cells, width, transcriptHeight, c.offset, c.expanded, c.session.takeDirtyIndex()),
    [layout, c.session.contentRevision, width, transcriptHeight, c.offset, c.expanded]);
  c.viewportRows = Math.max(1, panel ? panelHeight : transcriptHeight); c.totalRows = transcript.total;
  if (c.offset !== null) c.offset = transcriptHeight > 0 && transcript.start + transcriptHeight >= transcript.total ? null : transcript.start;
  const state = c.session.state;
  const backgroundCount = [...c.session.processes.values()].filter(process => process.state !== 'exited').length;
  const shortcutHints = (panel
    ? [keyHint('Esc', 'back'), keyHint('PgUp/PgDn', 'scroll'), keyHint('F2', 'details')]
    : [...(!dimensions.sidebar && backgroundCount ? [keyHint('/ps', `${backgroundCount} processes`)] : []), keyHint('F4', c.expanded ? 'collapse details' : 'tool output + reasoning + LSP'), keyHint('F2', 'session'), keyHint('/', 'commands'), ...(dimensions.width >= 100 ? [keyHint('Ctrl+C', state.running ? 'interrupt' : 'exit')] : [])]
  ).join('   ');
  const footer = c.exitConfirm ? paint.warning('Press Ctrl+C again to save and exit.') : c.session.fatal ? paint.error(safe(c.session.fatal)) : c.status ? paint.muted(safe(c.status)) : c.offset !== null ? paint.muted(`History ${transcript.start + 1}/${transcript.estimated ? '~' : ''}${transcript.total} · End follows output`) : shortcutHints;
  const inputColor = focused ? paint.accent : paint.muted;
  const panelColor = c.active ? paint.warning : paint.accent;
  const composer = composerInput.rows.map((row, index) => frameRow(
    (index ? '  ' : inputColor('› ')) + row + (!c.composer.text && !index && focused ? paint.muted('Describe your next change…') : ''), dimensions.width,
  ));
  const composerAction = focused ? paint.badge(state.running ? 'Enter queue' : 'Enter send') : '';
  const composerHint = focused ? `Alt+Enter newline${dimensions.width >= 65 ? ' · Alt+↑↓ history' : ''}` : 'Draft preserved';
  const welcome = [
    keyHint('Enter', state.running ? 'queue a follow-up prompt' : 'send a prompt'),
    keyHint('/', 'explore commands') + (width >= 45 ? '   ' + keyHint('Ctrl+G', 'keyboard help') : ''),
  ];
  const emptyRows = [...Array.from({length: Math.max(0, Math.floor((transcriptHeight - welcome.length) / 3))}, () => ''), ...welcome];
  const panelKind = c.active ? c.active.kind === 'review' || c.active.kind === 'confirm' ? 'REVIEW' : 'INPUT' : c.screen?.kind === 'document' ? 'DETAILS' : c.screen?.kind === 'form' ? 'CONFIGURE' : 'COMMANDS';
  const panelTransition = c.active ? `${c.active.request.request_id}:${c.interactionMode}`
    : `${c.screens.length}:${c.screen?.kind}:${c.screen?.title}:${c.screen?.kind === 'form' ? c.screen.index : ''}`;
  const sidebar = useMemo(() => dimensions.sidebar > 0
    ? <ProcessSidebar controller={c} height={bodyHeight} width={dimensions.sidebar}/> : null,
  [c, bodyHeight, dimensions.sidebar, c.session.state, c.session.sidebarRevision, c.session.plan, c.session.progress,
    c.session.git, c.session.fatal, c.active, liveActivity?.label]);
  // Coordinates include the root padding and the input's frame/rail prefix.
  setCursorPosition(composerInput.cursor ? {
    x: 5 + composerInput.cursor.x,
    y: chrome.header.length + bodyHeight + queue.length + 1 + composerInput.cursor.y,
  } : panel?.cursor && panel.cursor.y < panelHeight ? {
    x: 3 + panel.cursor.x,
    y: chrome.header.length + transcriptHeight + (liveActivity ? 1 : 0) + 1 + panel.cursor.y,
  } : undefined);
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} width={c.columns} height={height} overflowY="hidden">
    <Reveal key={c.session.fatal ? 'disconnected' : c.session.connected ? 'connected' : 'connecting'}>{progress =>
      <Rows rows={chrome.header.map(row => paint.reveal(row, progress))} height={chrome.header.length} width={dimensions.width}/>
    }</Reveal>
    <Box flexDirection="row" height={bodyHeight} flexShrink={0}>
      <Box flexDirection="column" width={width} flexShrink={0}>
        <Rows rows={transcript.rows.length ? transcript.rows : emptyRows} height={transcriptHeight} width={width}/>
        {liveActivity && <ActivityLine key={liveActivity.label} {...liveActivity} width={width}/>}
        {panel && <Reveal key={panelTransition}>{progress => <>
          <Rows rows={[paint.reveal(paint.panel(between(paint.badge(panelKind, c.active ? 'warning' : 'accent') + ' ' + paint.secondary(safe(panel.title)), paint.info(panel.navigation || ''), width)), progress)]} height={1} width={width}/>
          <Rows rows={Array.from({length: panelHeight}, (_, index) => paint.reveal(rail(panel.rows[index] || '', width, panelColor), progress))} height={panelHeight} width={width}/>
          <Rows rows={panelHints.map(hint => rail(hint, width, panelColor))} height={panelHints.length} width={width}/>
        </>}</Reveal>}
      </Box>
      {dimensions.sidebar > 0 && <>
        <Rows rows={Array.from({length: bodyHeight}, () => paint.border(' │ '))} height={bodyHeight} width={3}/>
        {sidebar}
      </>}
    </Box>
    {queue.length > 0 && <Rows rows={queue} height={queue.length} width={dimensions.width}/>}
    <ComposerEdge label={focused ? state.running ? 'YOU / STEERING' : 'YOU' : 'DRAFT'} detail={composerAction}
      width={dimensions.width} focused={focused}
      phase={c.session.fatal ? 'error' : c.active || state.approval_waiting ? 'attention' : liveActivity?.moving ? 'working' : 'idle'}/>
    <Rows rows={composer} height={composerHeight} width={dimensions.width}/>
    <Rows rows={[frameEdge(composerHint, dimensions.width, true, paint.muted)]} height={1} width={dimensions.width}/>
    <Rows rows={[footer, ...(chrome.status ? [chrome.status] : [])]} height={footerHeight} width={dimensions.width}/>
  </Box>;
}
