export interface SettledAttempt {
  attemptId: string;
  text: string;
  partial: boolean;
}
export interface StreamState {
  cursor: number;
  attemptId: string | null;
  attempt: number;
  state: 'idle' | 'open' | 'committed' | 'discarded' | 'interrupted';
  text: string;
  partial: boolean;
  settled: SettledAttempt[];
}
export interface StreamEvent {
  sequence: number;
  attempt_id: string;
  kind: 'begin' | 'delta' | 'settle';
  attempt?: number;
  state?: 'committed' | 'discarded' | 'interrupted';
  text?: string;
}
export function emptyStream(): StreamState;
export function replayStream(previous: StreamState, events: StreamEvent[]): StreamState;
