import {useEffect, useRef} from 'react';
import {useInput, useStdin, useStdout} from 'ink';
import type {EventEmitter} from 'node:events';
import type {TuiController} from '../state/controller.js';

/** Consume terminal reports before Ink's keyboard projection can turn them into text. */
export function terminalEvent(controller: TuiController, data: string): boolean {
  if (data.startsWith('\x1b[<')) {
    const mouse = /^\x1b\[<(\d+);([1-9]\d*);([1-9]\d*)([Mm])$/.exec(data);
    if (mouse && mouse[4] === 'M') {
      const button = Number(mouse[1]) & ~28; // Shift, Alt and Ctrl do not change the target.
      if (button === 64 || button === 65) controller.wheel(button === 64 ? -3 : 3);
    }
    return true; // Buttons, releases and malformed reports must never edit a draft.
  }
  if (/^\x1b(?:OP|\[11~|\[\[A|\[P)$/.test(data)) {controller.showHelp(); return true;}
  if (/^\x1b(?:OQ|\[12~|\[\[B|\[Q)$/.test(data)) {controller.showSession(); return true;}
  if (/^\x1b(?:OS|\[14~|\[\[D|\[S)$/.test(data)) {controller.toggleDetails(); return true;}
  return false;
}

/** Ink 7 omits raw sequences from useInput; isolate its pinned event bridge here. */
export function useTerminalInput(controller: TuiController) {
  const {internal_eventEmitter: emitter} = useStdin() as ReturnType<typeof useStdin> & {internal_eventEmitter: EventEmitter};
  const consumed = useRef(false);
  useEffect(() => {
    const handle = (data: string) => {consumed.current = terminalEvent(controller, data);};
    emitter.prependListener('input', handle);
    return () => {emitter.off('input', handle);};
  }, [controller, emitter]);
  useInput((input, key) => {if (!consumed.current) void controller.key(input, key).catch(controller.fail);});
}

/** No motion tracking: report wheel/buttons using SGR and keep arrows independent. */
export function useMouseReporting(alternateScreen: boolean, enabled: boolean) {
  const {stdout} = useStdout();
  useEffect(() => {
    if (!alternateScreen || !stdout.isTTY) return;
    stdout.write('\x1b[?1007l' + (enabled ? '\x1b[?1000h\x1b[?1006h' : ''));
    return () => {stdout.write('\x1b[?1000l\x1b[?1006l\x1b[?1007l');};
  }, [stdout, alternateScreen, enabled]);
}
