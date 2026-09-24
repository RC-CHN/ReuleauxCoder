import assert from 'node:assert/strict';
import test from 'node:test';
import type {Panel} from '@reuleauxcoder/client';
import {coreText, setLocale, shortDuration, compactNumber, toolLabel, translate, type MessageKey} from '../src/i18n.js';
import {coreMessage, interactionText, approvalReason, approvalText} from '../src/core-messages.js';
import {panelBody, panelItemText, panelTitle, permissionTarget} from '../src/panel-i18n.js';
import {backend} from './helpers.js';

const panel = (view_type: string, extra: Partial<Panel> = {}): Panel => ({view_type, title: '', items: [], children: [], filterable: false, keep_open_on_submit: false, return_to_parent_on_submit: false, ...extra});
const row = (label: string, description: string, action_id?: string): Panel['items'][number] => ({label, description, current: false, action: action_id ? {action_id, command: {}} : null});

test('panel translations preserve custom names, goals, IDs, shell commands and preview content', () => {
  setLocale('zh-CN');
  try {
    const profile = row('code', 'enabled · ctx 128000', 'model.use_main');
    assert.deepEqual(panelItemText(panel('model_profiles'), profile), {label: 'code', description: 'enabled · 上下文：128000'});
    assert.equal(panelTitle(panel('model_profiles', {title: 'Model Profiles · Session · Main model'})), '模型配置 · 当前会话 · 主模型');
    assert.deepEqual(panelItemText(panel('model_slots'), row('Session · Main model', 'code')), {label: '当前会话 · 主模型', description: 'code'});
    assert.deepEqual(panelItemText(panel('skills'), row('enabled', 'enabled · user · Running · code', 'skills.disable')), {label: 'enabled', description: '已启用 · 用户级 · Running · code'});
    assert.deepEqual(panelItemText(panel('mcp_servers'), row('workspace', 'enabled · active · g3 · 12 tools · error=CustomError', 'mcp.disable')), {label: 'workspace', description: '已启用 · 可用 · 连接代次 3 · 12 个工具 · 错误：CustomError'});
    assert.equal(panelItemText(panel('goal'), row('Active', 'Create goal')).description, 'Create goal');
    assert.deepEqual(panelItemText(panel('goal'), row('Tokens', '1,200 · No limit · 30 estimated requests')), {label: 'Token 用量', description: '1,200 · 不限 · 预计还可请求 30 次'});
    assert.deepEqual(panelItemText(panel('goal'), row('Elapsed', '35 seconds')), {label: '耗时', description: '35 秒'});
    const session = {...row('#1 2026-09-24', 'enabled · code · sid [active]', 'sessions.resume'), current: true};
    assert.equal(panelItemText(panel('sessions'), session).description, 'enabled · code · sid [当前会话]');
    const command = 'echo "Directory: enabled"';
    const process = panel('process_session:code', {title: `running · local · ${command}`, body: `${command}\nrunning · 12.5s · local/pty\nDirectory: /tmp/Running\nSession: code\nExit code: 0\nSome process output was truncated.`, output: 'enabled\nDirectory: code'});
    const original = JSON.stringify(process);
    assert.equal(panelTitle(process), `执行中 · local · ${command}`);
    assert.equal(panelBody(process), `${command}\n执行中 · 12.5秒 · local/pty\n目录：/tmp/Running\n会话：code\n退出码：0\n部分进程输出已截短。`);
    assert.equal(JSON.stringify(process), original);
    const multiline = {...process, body: `echo 'first line\nDirectory: enabled\nSession: code'\n${process.body!.split('\n').slice(1).join('\n')}`};
    assert(panelBody(multiline).startsWith("echo 'first line\nDirectory: enabled\nSession: code'\n执行中"));
    assert.equal(panelBody({...process, body: 'Directory: enabled\nSession: code'}), 'Directory: enabled\nSession: code');
    assert.deepEqual(panelItemText(panel('process_sessions'), {...row(command, 'running · 12.5s · local/pty'), id: 'p1'}), {label: command, description: '执行中 · 12.5秒 · local/pty'});
    assert.equal(permissionTarget('MCP · enabled · code'), 'MCP · enabled · code');
    assert.equal(permissionTarget('Effect · code/active.ts'), '影响类型 · code/active.ts');
    assert.equal(toolLabel('code'), 'code'); assert.equal(toolLabel('web_search'), '搜索网页');
  } finally {setLocale('en');}
});

test('known notices and confirmation wrappers translate without changing embedded payloads', () => {
  setLocale('zh-CN');
  try {
    assert.equal(coreMessage("Switched session main model profile to 'code' (provider/enabled)"), '当前会话主模型已切换为“code”（provider/enabled）');
    assert.equal(coreMessage('Updated workspace approval rule and saved to C:\\My Code\\config.yaml'), '已更新工作区权限规则，并保存到 C:\\My Code\\config.yaml');
    assert.equal(coreMessage("Skill 'enabled' disabled."), '技能“enabled”已停用。');
    assert.equal(coreMessage("MCP server 'code' enabled and saved to /workspace/enabled/config.yaml"), 'MCP 服务“code”已启用，已保存到 /workspace/enabled/config.yaml');
    assert.equal(coreMessage('Goal paused · Tokens 1,234 / No limit'), '目标：已暂停 · Token 用量：1,234 / 不限');
    assert.equal(coreMessage('Job code completed.\nRunning\nSession saved: user text'), '子任务 code 已完成。\nRunning\nSession saved: user text');
    const subject = 'echo enabled\nlocal · /tmp/code\nRunning';
    assert.equal(interactionText(`Stop this process and its descendants?\n\n${subject}`), `停止此进程及其子进程？\n\n${subject}`);
    assert.equal(interactionText('Hidden input · code'), '隐藏输入 · code');
    assert.equal(interactionText('blank cancels'), '留空即可取消');
    assert.equal(coreText('Targets: enabled'), '目标: enabled');
    assert.equal(coreText('node -e "enabled · running"'), 'node -e "enabled · running"');
    assert.equal(coreMessage('custom provider error: code · enabled'), 'custom provider error: code · enabled');
    assert.equal(coreMessage('code'), 'code'); assert.equal(approvalReason('enabled'), 'enabled'); assert.equal(approvalText('code'), 'code');
    assert.equal(approvalText("Tool 'read_file' from source 'builtin' requires approval.\nTargets: /tmp/enabled"), '工具 read_file（来源：内置工具）需要你的许可。\n目标: /tmp/enabled');
    assert.equal(shortDuration(45), '45秒'); assert.equal(shortDuration(125), '2分钟'); assert.equal(shortDuration(7201), '2小时');
    assert.equal(compactNumber(27420), '2.7万');
  } finally {setLocale('en');}
  for (const text of ['This 3 files', 'Source: sub-agent (mode=coder)', 'Goal paused · Tokens 1,234 / No limit', 'Hidden input · code', 'custom provider error: code · enabled']) {
    assert.equal(coreText(text), text); assert.equal(coreMessage(text), text); assert.equal(interactionText(text), text);
  }
  assert.equal(shortDuration(125), '2m');
  const item = row('code', 'enabled · user · code', 'skills.disable');
  assert.deepEqual(panelItemText(panel('skills'), item), {label: item.label, description: item.description});
});

test('real built-in command catalog has Chinese action descriptions and form labels', async t => {
  const b = await backend(); t.after(() => b.close());
  const missing: string[] = [];
  for (const action of b.client.catalog) {
    const description = action.description.replace(/^(?:\[[^\]]+\]\s*)+/, '');
    if (translate('zh-CN', description as MessageKey) === description) missing.push(`${action.action_id}: ${description}`);
    for (const parameter of action.parameters) if (translate('zh-CN', parameter.name as MessageKey) === parameter.name) missing.push(`${action.action_id}.${parameter.name}`);
  }
  assert.deepEqual(missing, []);
});
