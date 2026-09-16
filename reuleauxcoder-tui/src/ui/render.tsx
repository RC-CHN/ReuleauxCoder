import React, {useContext, useInsertionEffect, useMemo, type ReactNode} from 'react';
import {render, type Instance, type RenderOptions} from 'ink';
// Ink 7.1.1 has no public per-frame cursor setter. Keep this compatibility
// boundary here; application inputs continue to use the public useCursor hook.
import CursorContext from '../../node_modules/ink/build/components/CursorContext.js';
import type {CursorPosition} from './viewport.js';

interface CursorFrame {
  position?: CursorPosition;
  target?: React.ContextType<typeof CursorContext>;
}

function CursorRoot({frame, children}: {frame: CursorFrame; children: ReactNode}) {
  const target = useContext(CursorContext);
  const context = useMemo(() => ({setCursorPosition(position: CursorPosition | undefined) {
    // useCursor calls this during commit, so abandoned renders cannot move IME.
    frame.position = position;
    target.setCursorPosition(position);
  }}), [frame, target]);
  useInsertionEffect(() => {
    frame.target = target;
    return () => {frame.target = undefined; frame.position = undefined;};
  }, [frame, target]);
  return <CursorContext.Provider value={context}>{children}</CursorContext.Provider>;
}

/** Own terminal frame behavior without making input owners rerender for animations. */
export function renderTerminal(node: ReactNode, options: RenderOptions = {}): Instance {
  const frame: CursorFrame = {};
  const wrap = (content: ReactNode) => <CursorRoot frame={frame}>{content}</CursorRoot>;
  const instance = render(wrap(node), {...options, onRender(metrics) {
    // Ink invokes onRender after layout, before writing the frame. Its cursor
    // intent is consumed by each write, including child-only commits and resize.
    frame.target?.setCursorPosition(frame.position);
    options.onRender?.(metrics);
  }});
  return {...instance, rerender: content => instance.rerender(wrap(content))};
}
