import type { Chat } from './types';

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
