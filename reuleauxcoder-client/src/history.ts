export interface HistoryRecord {
  event_id: string; seq: number; turn_id: string | null; kind: string; role: string | null;
  content: string; offset: number; total_chars: number; next_offset: number | null; artifact_refs: string[];
}
export interface HistoryPage {
  session_id: string; records: HistoryRecord[]; next_cursor: string | null;
  indexed_bytes: number; source_bytes: number; skipped_records: number; awaiting_tail: boolean;
}
export interface ArtifactPage {
  session_id: string; artifact_ref: string; content: string; offset: number;
  next_cursor: string | null; next_offset: number | null; total_chars: number | null;
}
export type HistoryOperation = 'read' | 'search' | 'artifact';
