export interface ChatCell {id: string; role: 'user' | 'assistant' | 'reasoning' | 'tool' | 'notice'; text: string; title?: string; status?: string; detail?: string}
export interface DraftItem {id: string; name: string; kind: 'context' | 'file' | 'image'; text?: string; reference?: any}
export interface ReviewSummary {id: string; title: string; summary: string; documents: {id: string; path: string}[]}
export interface HostSnapshot {
  hostId: string; revision: number; draftRevision: number;
  phase: 'idle' | 'starting' | 'ready' | 'failed' | 'installing' | 'stopping';
  environment: string; workspace: string; generation: number; model: string; running: boolean;
  cells: ChatCell[]; reviews: ReviewSummary[]; draftItems: DraftItem[]; draftText: string;
  error?: {kind: string; message: string}; notice?: string;
}
export interface WebRequest {id: string; action: string; data?: Record<string, any>}
