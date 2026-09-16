import React, {useState} from 'react';
import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough, Writable} from 'node:stream';
import {Box, Text, useCursor} from 'ink';
import {renderTerminal} from '../src/ui/render.js';
import {until} from './helpers.js';

for (const incrementalRendering of [false, true]) {
  test(`terminal retains the input cursor across child commits (incremental=${incrementalRendering})`, async t => {
    const writes: string[] = [];
    const stdout = Object.assign(new Writable({write(chunk, _encoding, done) {
      writes.push(chunk.toString()); done();
    }}), {columns: 80, rows: 24, isTTY: true});
    const stdin = Object.assign(new PassThrough(), {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
    let update: (value: number) => void;
    let parentRenders = 0;
    function Output() {
      const [value, setValue] = useState(0); update = setValue;
      return <Text>{`output ${value}`}</Text>;
    }
    function Screen({focused = true, x = 4}: {focused?: boolean; x?: number}) {
      parentRenders++;
      useCursor().setCursorPosition(focused ? {x, y: 1} : undefined);
      return <Box flexDirection="column"><Output/><Text>input</Text><Text>footer</Text></Box>;
    }
    const app = renderTerminal(<Screen/>, {stdout, stdin, stderr: stdout, interactive: true,
      exitOnCtrlC: false, patchConsole: false, incrementalRendering, maxFps: 60});
    t.after(() => {app.unmount(); app.cleanup(); stdin.destroy();});
    const frame = () => writes.filter(value => value.includes('output')).at(-1)!;
    await until(() => frame()?.includes('output 0'));
    assert(frame().endsWith('\x1b[2A\x1b[5G\x1b[?25h'));
    const initialRenders = parentRenders;
    for (const value of [1, 2, 3]) {
      update!(value);
      await until(() => frame()?.includes(`output ${value}`));
      assert.equal(parentRenders, initialRenders, 'local output does not rerender the input owner');
      assert(frame().endsWith('\x1b[2A\x1b[5G\x1b[?25h'), 'each output frame ends at the input cursor');
    }
    writes.length = 0;
    app.rerender(<Screen x={7}/>);
    await until(() => writes.some(value => value.endsWith('\x1b[8G\x1b[?25h')));
    writes.length = 0;
    stdout.columns = 60; stdout.emit('resize');
    await until(() => frame()?.includes('output 3'));
    assert(frame().endsWith('\x1b[2A\x1b[8G\x1b[?25h'), 'resize replays the committed cursor after clearing output');
    writes.length = 0;
    app.rerender(<Screen focused={false}/>);
    await until(() => writes.join('').includes('\x1b[?25l'));
    update!(4);
    await until(() => frame()?.includes('output 4'));
    assert(!frame().includes('\x1b[?25h'), 'unfocused input must not restore an old cursor');
  });
}
