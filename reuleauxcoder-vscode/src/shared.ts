import type {Action, Panel, Goal, GitWorkspace} from '@reuleauxcoder/client';
import type {ConfigChange, ConfigDiagnostic, ConfigValidation, ConfigScope} from '@reuleauxcoder/client';
export interface RecoverySnapshot {
  busy: boolean; valid?: boolean; error?: string; message?: string;
  sources: {scope: ConfigScope; path: string; exists: boolean}[];
  diagnostics: ConfigDiagnostic[];
  history: {id: string; date: string; path: string; scope: ConfigScope}[];
  candidate?: ConfigChange; validation?: ConfigValidation;
}
export interface WorkOverview {
  goal?: Goal | null; contextTokens: number; contextLimit: number; approvalPolicy: string; mcpTools: number; queued: number;
  plan: {step: string; status: string}[]; progress: string; activity: string;
  jobs: {id: string; task: string; status: string; detail: string}[];
  processes: {id: string; command: string; state: string; elapsed: number; output: string}[];
  diagnostics: {path: string; errors: number; warnings: number}[];
  git?: GitWorkspace | null; warnings: string[];
}
export interface CommandSurface {id: number; feature: string; busy: boolean; panel?: Panel; action?: Action; canBack: boolean}
export interface InlineInteraction {id: string; kind: string; title: string; message: string; secret?: boolean; initial?: string; placeholder?: string; allowEmpty?: boolean; allowCancel: boolean; items?: {id: string; label: string; description: string}[]}
export interface ChatCell {id: string; role: 'user' | 'assistant' | 'reasoning' | 'tool' | 'notice'; text: string; title?: string; status?: string; detail?: string}
export interface DraftItem {id: string; name: string; kind: 'context' | 'file' | 'image'; text?: string; reference?: any}
export interface ReviewSummary {
  id: string; title: string; summary: string; documents: {id: string; path: string}[];
  dirty?: boolean; grants?: {id: string; label: string; description: string; broad: boolean}[]; context?: string;
  tool?: string; source?: string; reason?: string; cwd?: string | null;
  preview?: {title: string; content: string; truncated: boolean; secondary?: boolean}[];
}
export interface HostSnapshot {
  hostId: string; revision: number; draftRevision: number;
  phase: 'idle' | 'starting' | 'ready' | 'failed' | 'installing' | 'stopping';
  environment: string; workspace: string; generation: number; model: string; running: boolean;
  cells: ChatCell[]; reviews: ReviewSummary[]; draftItems: DraftItem[]; draftText: string;
  error?: {kind: string; message: string}; notice?: string;
  catalog?: Action[]; commandSurface?: CommandSurface; interactions?: InlineInteraction[]; mode?: string;
  overview?: WorkOverview;
  recovery?: RecoverySnapshot;
}
export interface WebRequest {id: string; action: string; data?: Record<string, any>}
