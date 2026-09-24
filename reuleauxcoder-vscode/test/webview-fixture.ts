import type {Action, Panel} from '@reuleauxcoder/client';
import type {WorkOverview} from '../src/shared.js';

export const catalog: Action[] = [
  {action_id: 'goal.show', feature_id: 'goal', description: 'View goal and controls', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/goal'}]},
  {action_id: 'goal.create', feature_id: 'goal', description: 'Create a persistent goal', preview: false, parameters: [{name: 'objective', kind: 'text', required: true, nullable: false, default: null}], triggers: [{kind: 'slash', value: '/goal create'}]},
  {action_id: 'model.show', feature_id: 'model', description: 'Model Profiles', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/model'}]},
  {action_id: 'mode.show', feature_id: 'mode', description: 'Modes', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/mode'}]},
  {action_id: 'skills.show', feature_id: 'skills', description: 'Skills', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/skills'}]},
  {action_id: 'mcp.show', feature_id: 'mcp', description: 'MCP Servers', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/mcp'}]},
  {action_id: 'approval.show', feature_id: 'approval', description: 'Permissions', preview: true, parameters: [], triggers: [{kind: 'slash', value: '/approval'}]},
];
const base: Panel = {view_type: 'models', title: 'Model Profiles', items: [], children: [], filterable: true, keep_open_on_submit: true, return_to_parent_on_submit: false};
export const panels: Record<string, Panel> = {
  'model.show': {...base, items: [{label: 'Session · Main model', description: 'Reasoning model', action: null, current: false}], children: [['Session · Main model', {...base, view_type: 'profiles', items: [{label: 'Reasoning model', description: '128k context', current: true, action: {action_id: 'model.use_main', command: {profile_name: 'main'}}}, {label: 'Fast model', description: '64k context', current: false, action: {action_id: 'model.use_main', command: {profile_name: 'fast'}}}]}]]},
  'skills.show': {...base, view_type: 'skills', title: 'Skills', items: [{label: 'Code review', description: 'Review code for bugs and regressions', current: true, action: {action_id: 'skills.disable', command: {skill_name: 'review', enabled: false}}}]},
  'goal.show': {...base, view_type: 'goal', title: 'Goal', items: [{label: 'Create goal', description: 'Keep working toward an objective', current: false, action: {action_id: 'goal.create', command: {objective: null}}}]},
  'approval.show': {...base, view_type: 'approval', title: 'Permissions', items: [{label: 'This session', description: 'Ask', current: true, action: null}]},
};
export const overview: WorkOverview = {
  contextTokens: 27420, contextLimit: 128000, approvalPolicy: 'require_approval', mcpTools: 12, queued: 0,
  goal: {id: 'goal', objective: 'Improve the workspace experience', status: 'active', token_budget: 80000, tokens_used: 18420, time_used_seconds: 246, created_at: 0, updated_at: 0, estimated_requests: 0},
  plan: [{step: 'Inspect input and command flow', status: 'completed'}, {step: 'Refine the conversation interface', status: 'in_progress'}, {step: 'Verify on Windows and Remote', status: 'pending'}],
  progress: 'The input flow is ready. Reviewing the proposed changes.', activity: 'edit_file',
  jobs: [{id: 'audit', task: 'Check interaction lifecycle', status: 'running', detail: 'Inspecting cancellation and stale requests'}],
  processes: [{id: 'test', command: 'npm run test:webview', state: 'running', elapsed: 18, output: '✓ steering keeps the next draft\n✓ Markdown links are safe'}],
  diagnostics: [], warnings: [],
  git: {available: true, branch: 'main', head: 'abc', upstream: 'origin/main', ahead: 0, behind: 0, additions: 126, deletions: 38, truncated: false, reason: null, files: [{path: 'src/webview/main.ts', index: '.', worktree: 'M', conflict: false}, {path: 'src/webview/style.css', index: '.', worktree: 'M', conflict: false}]},
};
