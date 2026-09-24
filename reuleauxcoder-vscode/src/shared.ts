import type {Action, Panel, Goal, GitWorkspace} from '@reuleauxcoder/client';
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
export interface ReviewSummary {id: string; title: string; summary: string; documents: {id: string; path: string}[]; dirty?: boolean; grants?: {id: string; label: string; description: string; broad: boolean}[]; context?: string}
export interface HostSnapshot {
  hostId: string; revision: number; draftRevision: number;
  phase: 'idle' | 'starting' | 'ready' | 'failed' | 'installing' | 'stopping';
  environment: string; workspace: string; generation: number; model: string; running: boolean;
  cells: ChatCell[]; reviews: ReviewSummary[]; draftItems: DraftItem[]; draftText: string;
  error?: {kind: string; message: string}; notice?: string;
  catalog?: Action[]; commandSurface?: CommandSurface; interactions?: InlineInteraction[]; mode?: string;
  overview?: WorkOverview;
}
export interface WebRequest {id: string; action: string; data?: Record<string, any>}
