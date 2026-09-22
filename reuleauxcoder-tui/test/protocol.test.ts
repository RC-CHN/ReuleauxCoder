import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import {once} from 'node:events';
import {RpcPeer, RpcError} from '../src/protocol/peer.js';
import {RuntimeClient} from '../src/protocol/client.js';
import {decode, record} from '../src/protocol/wire.js';

test('shutdown keeps the connection open through a slow save and delivers progress', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const input = new PassThrough(), output = new PassThrough();
  const peer = new RpcPeer(input, output); t.after(() => peer.close());
  const client = new RuntimeClient(peer);
  const written: any[] = []; output.on('data', chunk => written.push(JSON.parse(chunk.toString())));
  const progress: string[] = []; client.on('shutdownProgress', message => progress.push(message));
  const shutdown = client.shutdown();
  let completed = false;
  void shutdown.then(() => {completed = true;});
  const ordinary = assert.rejects(peer.request('ordinary'), /Request timed out: ordinary/);
  t.mock.timers.tick(60_000);
  await ordinary;
  assert(!completed);
  assert(!peer.closed, 'a slow save must not trigger process teardown');
  input.write(JSON.stringify({jsonrpc: '2.0', method: 'runtime.shutdown_progress', params: {message: 'Committing session manifest...'}}) + '\n');
  assert.deepEqual(progress, ['Committing session manifest...']);
  input.write(JSON.stringify({jsonrpc: '2.0', id: written[0].id, result: 'saved-session'}) + '\n');
  assert.equal(await shutdown, 'saved-session');
  assert(peer.closed);
});

test('shutdown still fails on backend errors, disconnects and explicit host close', async () => {
  for (const cause of ['error', 'disconnect', 'close']) {
    const input = new PassThrough(), output = new PassThrough();
    const peer = new RpcPeer(input, output);
    const client = new RuntimeClient(peer);
    const rejected = assert.rejects(client.shutdown(), cause === 'error' ? /disk failed/ : cause === 'disconnect' ? /Backend disconnected/ : /connection lost/);
    if (cause === 'error') input.write(JSON.stringify({jsonrpc: '2.0', id: '1', error: {code: -32603, message: 'disk failed'}}) + '\n');
    else if (cause === 'disconnect') input.end();
    else peer.close(new Error('connection lost'));
    await rejected;
    assert(peer.closed);
  }
});

test('fragmented UTF-8 framing permits reverse requests and notifications while awaiting input', async t => {
  const input = new PassThrough(); const output = new PassThrough();
  const peer = new RpcPeer(input, output); t.after(() => peer.close());
  const client = new RuntimeClient(peer);
  const written: any[] = []; output.on('data', chunk => written.push(JSON.parse(chunk.toString())));
  const pending = peer.request('slow');
  const frame = Buffer.from(JSON.stringify({jsonrpc: '2.0', id: 'reverse', method: 'interaction.request', params: {kind: 'confirm', request: record('ConfirmRequest', {request_id: 'ask', title: '确认', message: 'Continue?'})}}) + '\n');
  const waiting = once(client, 'interactions');
  for (const byte of frame) input.write(Buffer.from([byte]));
  await waiting;
  const command = once(client, 'command');
  input.write(JSON.stringify({jsonrpc: '2.0', method: 'runtime.command', params: {text: 'still live'}}) + '\n');
  assert.deepEqual(await command, ['still live']);
  input.write(JSON.stringify({jsonrpc: '2.0', id: written[0].id, result: '响应'}) + '\n');
  assert.equal(await pending, '响应');
  assert.equal(client.interactions[0].request.title, '确认');
  client.answer('ask', record('ConfirmResponse', {confirmed: true}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(decode(written.at(-1).result).confirmed, true);
});

test('malformed replies and EOF reject pending calls instead of leaving promises suspended', async () => {
  for (const frame of ['{"jsonrpc":"2.0","id":"1"}\n', '{"jsonrpc":"2.0","id":"1","error":null}\n', null]) {
    const input = new PassThrough(); const output = new PassThrough();
    const peer = new RpcPeer(input, output);
    const pending = peer.request('pending');
    const rejection = assert.rejects(pending);
    if (frame) input.write(frame); else input.end();
    await rejection; assert(peer.closed);
  }
});

test('large fragmented frames copy at most one frame and preserve adjacent messages', t => {
  const input = new PassThrough(), output = new PassThrough();
  const peer = new RpcPeer(input, output); t.after(() => peer.close());
  const received: unknown[] = [];
  peer.on('notification', (method, params) => received.push([method, params]));
  const text = '中文🙂'.repeat(100_000);
  const frame = Buffer.from(JSON.stringify({jsonrpc: '2.0', method: 'large', params: {text}}));
  const next = Buffer.from('\n{"jsonrpc":"2.0","method":"next","params":{}}\n');
  const concat = Buffer.concat;
  let copiedBytes = 0;
  t.mock.method(Buffer, 'concat', (parts: Buffer[], length?: number) => {
    copiedBytes += parts.reduce((total, part) => total + part.length, 0);
    return concat(parts, length);
  });
  for (let offset = 0; offset < frame.length; offset += 1024) input.write(frame.subarray(offset, offset + 1024));
  assert.equal(copiedBytes, 0, 'incomplete frames never copy their accumulated prefix');
  input.write(next);
  assert.deepEqual(received, [['large', {text}], ['next', {}]]);
  assert.equal(copiedBytes, frame.length);
});

test('frame limits apply to each fragmented message, including the final fragment', t => {
  for (const newline of [false, true]) {
    const input = new PassThrough(), output = new PassThrough();
    const peer = new RpcPeer(input, output); t.after(() => peer.close());
    let failure: Error | undefined;
    peer.on('close', error => {failure = error;});
    input.write(Buffer.alloc(RpcPeer.maxFrame, 32));
    assert(!peer.closed, 'a frame at the limit may still receive its newline');
    input.write(Buffer.from(newline ? 'x\n' : 'x'));
    assert(peer.closed);
    assert.match(failure!.message, /exceeds 16 MiB/);
  }
});

test('reverse interaction deadline and cancellation resolve the same correlated response', async t => {
  const a = new PassThrough(); const b = new PassThrough();
  const frontend = new RpcPeer(a, b); const backend = new RpcPeer(b, a);
  t.after(() => {frontend.close(); backend.close();});
  const client = new RuntimeClient(frontend);
  const request = record('InputTextRequest', {request_id: 'text', title: 'Timed', prompt: 'Value'});
  const timed = decode(await backend.request('interaction.request', {kind: 'input_text', request, timeout_seconds: 0.01}));
  assert(timed.cancelled); assert.equal(client.interactions.length, 0);
  const waiting = once(client, 'interactions');
  const pending = backend.request('interaction.request', {kind: 'input_text', request});
  await waiting;
  backend.notify('interaction.cancel', {request_id: 'text'});
  assert(decode(await pending).cancelled);
  await assert.rejects(backend.request('missing'), (error: unknown) => error instanceof RpcError && error.code === -32601);
});
