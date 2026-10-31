import type { Chat, Message } from './types';

/**
 * Phase 44 (1.9.1): adds the messages the host stored after `after` that this transcript does not show yet, right after
 * `after` and in the host's order; returns how many. A runtime command is answered at once (app.send): the host stores
 * the command and its reply, emits `complete` and starts no task, so no token or idle event brings the reply to a window.
 * The window reads the conversation back and adds what follows the command. A message already shown (a reply still
 * streaming for a task) is kept as it is.
 */
export function addStoredAfter(chat: Chat, stored: unknown, after: string): number {
  if (!Array.isArray(stored)) return 0;
  const from = stored.findIndex((m) => m?.id === after);
  if (from < 0) return 0;
  const shown = new Set(chat.messages.map((m) => m.id));
  const added = stored.slice(from + 1).filter((m): m is Message =>
    !!m && typeof m.id === 'string' && !shown.has(m.id) && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string');
  if (!added.length) return 0;
  const at = chat.messages.findIndex((m) => m.id === after);
  chat.messages.splice(at < 0 ? chat.messages.length : at + 1, 0, ...added);
  return added.length;
}

// Host events update task UI without replacing a streaming transcript.
export function applyRunEvent(chat: Chat, event: string, data: any): boolean {
  if (data.chat_id !== chat.id) return false;
  if (event === 'question') {
    const id = data.question_id;
    chat.questions = (chat.questions || []).filter(q => q.id !== id);
    if (data.question?.status === 'pending') chat.questions.push(data.question);
    if (data.message && !chat.messages.some(m => m.id === data.message.id)) chat.messages.push(data.message);
    return true;
  }
  if (event === 'plan') {
    chat.plan = data;
    return true;
  }
  if (event === 'run') {
    chat.runs ||= [];
    const run = chat.runs.find(r => r.id === data.run_id);
    if (run) run.state = data.state;
    else chat.runs.unshift({ id: data.run_id, state: data.state });
    return true;
  }
  return false;
}
