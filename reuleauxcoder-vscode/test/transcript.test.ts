import assert from 'node:assert/strict';
import test from 'node:test';
import {MessagePeer, RuntimeClient, decode, emptyState, record} from '@reuleauxcoder/client';
import {Transcript} from '../src/core/transcript.js';

function fixture() {
  const client = new RuntimeClient(new MessagePeer(() => {})), transcript = new Transcript();
  client.state = {...emptyState, agent_id: 'agent', running: true};
  transcript.bind(client); client.emit('state', client.state);
  const event = (type: string, fields: any) => client.emit('event', {payload: decode(record('RuntimeEventPayload', {event: record('RuntimeEvent', {agent_id: 'agent', payload: record(type, fields)})}))}, undefined, 0);
  return {client, transcript, event, close() {transcript.dispose(); client.close();}};
}

test('startup recovery warnings are visible in the conversation without opening the overview', () => {
  const f = fixture();
  try {
    f.client.emit('initialized', {recent_conversation: [{role: 'user', content: 'Earlier task'}], startup_events: [{level: 'info', message: 'Connected'}, {level: 'warning', message: 'Unreadable session skipped'}, {level: 'error', message: 'Another startup diagnostic'}]});
    assert.deepEqual(f.transcript.cells.map(cell => [cell.role, cell.text]), [['user', 'Earlier task'], ['notice', 'Unreadable session skipped'], ['notice', 'Another startup diagnostic']]);
  } finally {f.close();}
});

test('application proof places steering after preceding output, preserving images and distinct identical messages', () => {
  const f = fixture(), t = f.transcript;
  try {
    f.event('TurnStarted', {user_input: 'Work', submission_id: 'start'});
    f.event('AssistantContentDelta', {text: 'Before'});
    t.submission('a', 'Same instruction', 'sending');
    t.submission('a', 'Same instruction', 'queued');
    assert.deepEqual(t.cells.map(cell => cell.text), ['Work', 'Before']);
    const images = [{attachment_id: 'image', variant_id: 'variant'}] as any;
    t.images('a', images);
    f.event('AssistantContentDelta', {text: ' applying'});
    f.event('UserSteeringApplied', {user_input: 'Same instruction', submission_id: 'a'});
    f.event('AssistantContentDelta', {text: 'After'});
    t.submission('a', 'Same instruction', 'queued'); // Late acknowledgement.
    f.event('UserSteeringApplied', {user_input: 'Same instruction', submission_id: 'a'});
    f.event('AssistantContentDelta', {text: ' applying'});
    assert.deepEqual(t.cells.map(cell => cell.text), ['Work', 'Before applying', 'Same instruction', 'After applying']);
    assert.deepEqual(t.cells[2].images, images); assert.equal(t.pendingInputs.size, 0);
    t.submission('b', 'Same instruction', 'queued');
    f.event('ToolCallStarted', {tool_call_id: 'tool', tool_name: 'read_file', arguments: {path: 'a.ts'}});
    f.event('ToolCallFinished', {tool_call_id: 'tool', outcome: {status: 'succeeded', content: 'File content'}});
    f.event('UserSteeringApplied', {user_input: 'Same instruction', submission_id: 'b'});
    assert.deepEqual(t.cells.slice(-2).map(cell => cell.role), ['tool', 'user']);
    assert.equal(t.cells.filter(cell => cell.text === 'Same instruction').length, 2);
  } finally {f.close();}
});

test('stopped, unconfirmed and stale inputs never become conversation entries', () => {
  const f = fixture(), t = f.transcript;
  try {
    t.submission('a', 'Not applied', 'queued');
    f.client.emit('state', {...f.client.state, running: false});
    assert.equal(t.pendingInputs.get('a')?.status, 'not-applied');
    t.submission('b', 'Late receipt', 'queued');
    t.submission('c', 'Unconfirmed', 'unconfirmed');
    assert.equal(t.pendingInputs.get('b')?.status, 'not-applied');
    f.client.emit('state', f.client.state); // A new task starts before an old receipt arrives.
    t.submission('b', 'Late receipt', 'queued');
    assert.equal(t.pendingInputs.get('b')?.status, 'not-applied');
    assert.equal(t.cells.length, 0);
    f.client.emit('state', {...f.client.state, session_generation: 1});
    f.event('UserSteeringApplied', {user_input: 'Not applied', submission_id: 'a'});
    assert.equal(t.pendingInputs.size, 0); assert.equal(t.cells.length, 0);
  } finally {f.close();}
});
