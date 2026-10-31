export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  created?: number;
  usage?: Usage;
  attachments?: Attachment[];
  // Phase 22: the run that wrote this reply, when the host tags it (links "Thought for N s").
  run_id?: string;
  // Phase 40 (D93): small previews of the screenshots the run took, shown with the reply.
  captures?: { preview: string; width?: number; height?: number }[];
}
export interface Action {
  id: string;
  chat_id: string;
  kind: "edit" | "tests" | "app" | "command" | "plugin" | "git" | "worktree" | "install" | "shell" | "file"
    | "screen" | "screen_grant"  // Phase 42 (1.8.0): a press in a shared window; a window Pebble asked for
    | "url"  // Phase 43 (1.9.0): a web address opened in the person's browser
    | "screen_raise"  // Phase 43 (1.9.0): a granted window brought forward
    | "app_action"  // Phase 43 (1.9.0): a fixed scripting verb (Shortcuts, Music, Notes, Calendar, Mail, Finder)
    | "browser_grant";  // Phase 43 (1.9.0): sites in the person's browser Pebble asked for
  status: string;
  payload: Record<string, any>;
}
export interface Chat {
  id: string;
  title: string;
  workspace: string | null;
  messages: Message[];
  actions: Action[];
  artifacts?: Artifact[];
  runs?: RunSummary[];
  questions?: AgentQuestion[];
  plan?: AgentPlan | null;
  trashed?: boolean; // Phase 24 (V1-018): in Recently deleted
  updated?: number; // Phase 45 (F047): the chats row's own time (Unix seconds), shown in the companion's list
}
export interface RunSummary {
  id: string;
  state: string;
  steps?: number;
  elapsed?: number;
  error?: string | null;
  created?: number; // Unix seconds (runs.list)
  updated?: number;
  counts?: { steps: number; retries: number; waited: number } | null; // Phase 24 (V1-121): get_chat, settled runs only
}
export interface AgentQuestion {
  id: string;
  run_id: string;
  revision: number;
  required: boolean;
  status: string;
  requester_agent_id?: string | null;
  requester_agent_name?: string | null;
  parent_task_id?: string | null;
  origin?: string;
  items: {
    id: string;
    title: string;
    options: { id: string; label: string; description?: string }[];
    allow_free_text: boolean;
  }[];
}
export interface AgentPlan {
  run_id: string;
  revision: number;
  steps: {
    id: string;
    title: string;
    status: string;
    evidence_ids: string[];
  }[];
}
export type ArtifactStatus = "ready" | "review" | "stopped" | "expired";
export type ArtifactAnswer =
  { text: string } | { option_id: string; text?: string };
export type ArtifactAnswers = Record<string, ArtifactAnswer>;
export interface ArtifactFormQuestion {
  id: string;
  revision: number;
  status: "pending" | "answered" | "cancelled";
  answer?: ArtifactAnswers | null;
}
export interface ArtifactBase {
  id: string;
  chat_id: string;
  title: string;
  created: number;
  revision?: number;
  owner_run_id?: string | null;
  status?: ArtifactStatus;
  updated?: number;
  evidence_ids?: string[];
  data_hash?: string;
  form_question?: ArtifactFormQuestion | null;
}
export interface FormField {
  id: string;
  label: string;
  type: "text" | "choice";
  required: true;
  options?: { id: string; label: string }[];
  placeholder?: string;
}
export type ArtifactPrimitive = string | number | boolean | null;
export interface FormArtifactData {
  fields: FormField[];
  description?: string;
  submit_label?: string;
}
export interface TableArtifactData {
  columns: { id: string; label: string }[];
  rows: Record<string, ArtifactPrimitive>[];
  caption?: string;
}
export interface ChartArtifactData {
  // Phase 46 (1.9.29, D195): area to donut, the number format and a unit are the visual catalog's (chartModel.ts)
  type: "bar" | "line" | "area" | "scatter" | "stacked" | "pie" | "donut";
  x_key: string;
  y_keys: string[];
  rows: Record<string, ArtifactPrimitive>[];
  labels?: Record<string, string>;
  caption?: string;
  format?: "number" | "percent" | "compact";
  unit?: string;
}
// Phase 46 (1.9.29, D195): the visual catalog's kinds (artifact_schema.CATALOG_KINDS), drawn by VisualCatalog.vue
export type CatalogArtifact = ArtifactBase & { kind: "compare" | "metrics" | "timeline" | "record"; data: Record<string, any> };
export interface PlanArtifactData {
  steps: {
    id: string;
    title: string;
    status: "pending" | "in_progress" | "blocked" | "completed";
    evidence_ids?: string[];
  }[];
}
export interface EvidenceArtifactData {
  items: { id: string; label: string; evidence_id: string; summary?: string }[];
  caption?: string;
}
export type NativeArtifact = ArtifactBase &
  (
    | { kind: "form"; data: FormArtifactData }
    | { kind: "table"; data: TableArtifactData }
    | { kind: "chart"; data: ChartArtifactData }
    | { kind: "plan"; data: PlanArtifactData }
    | { kind: "evidence"; data: EvidenceArtifactData }
    | { kind: "compare" | "metrics" | "timeline" | "record"; data: Record<string, any> }
  );
// Quiz v2: the host keeps the answer key, so the view holds public questions only.
export interface QuizV2Question {
  id: string;
  question: string;
  choices: string[];
}
export interface QuizV2ArtifactData {
  duration_minutes: number;
  questions: QuizV2Question[];
}
export type QuizV2Artifact = ArtifactBase & { kind: "quiz"; data: QuizV2ArtifactData };
// Question id -> chosen index; null is unanswered.
export type QuizAnswers = Record<string, number | null>;
export type QuizAttemptStatus = "open" | "submitted" | "expired" | "superseded";
export interface QuizQuestionResult {
  id: string;
  correct: boolean;
  chosen?: number | null;
  answer?: number;
  explanation?: string;
}
export interface QuizResult {
  score: number;
  total: number;
  per_question: QuizQuestionResult[];
}
export interface QuizAttempt {
  attempt_id: string;
  deadline: number; // Unix seconds on the host clock
  status: QuizAttemptStatus;
  answers: QuizAnswers;
  artifact_id?: string;
  revision?: number;
  result?: QuizResult | null;
  shared?: boolean;
}
export type DashboardPanelKind = "metric" | "table" | "chart" | "plan" | "evidence";
export interface DashboardMetricData {
  value: number;
  unit?: string;
  delta?: number;
  caption?: string;
}
export type DashboardPanelData =
  | DashboardMetricData
  | TableArtifactData
  | ChartArtifactData
  | PlanArtifactData
  | EvidenceArtifactData;
export interface DashboardBinding {
  source: string;
  params?: Record<string, ArtifactPrimitive>;
}
export interface DashboardPanel {
  id: string;
  title: string;
  kind: DashboardPanelKind;
  data?: DashboardPanelData;
  binding?: DashboardBinding;
}
export interface DashboardArtifactData {
  panels: DashboardPanel[];
  columns?: 1 | 2 | 3;
}
// Host-resolved binding for one panel (added by artifact_get, never by the model).
export interface DashboardBindingResult {
  panel_id: string;
  source: string;
  as_of?: number; // Unix seconds
  digest?: string;
  data?: DashboardPanelData;
  error?: string;
}
export type DashboardArtifact = ArtifactBase & {
  kind: "dashboard";
  data: DashboardArtifactData;
  bindings?: DashboardBindingResult[];
};
export type PrototypeComponent =
  | { type: "heading"; text: string }
  | { type: "button"; label: string; goto?: string }
  | { type: "input"; label: string; placeholder?: string }
  | { type: "toggle"; label: string }
  | { type: "list"; items: string[] }
  | { type: "card"; title: string; body: string }
  | { type: "image_placeholder"; label: string; aspect?: string }
  | { type: "nav"; items: { label: string; goto: string }[] };
export interface PrototypeScreen {
  id: string;
  title: string;
  components: PrototypeComponent[];
}
export interface PrototypeArtifactData {
  viewport?: "phone" | "tablet" | "desktop";
  start: string;
  theme?: { accent?: string; density?: string; radius?: number };
  screens: PrototypeScreen[];
}
export type PrototypeArtifact = ArtifactBase & { kind: "prototype"; data: PrototypeArtifactData };
export interface PrototypeFeedback {
  chat_id: string;
  artifact_id: string;
  revision: number;
  screen_id: string;
}
export type Artifact =
  | NativeArtifact
  | DashboardArtifact
  | PrototypeArtifact
  | (ArtifactBase & {
      kind: "quiz" | "diagram" | "html" | "mcp_media";
      data: Record<string, any>;
    });
export interface ModelStatus {
  state: string;
  configured: boolean;
  name: string;
  path: string;
  context: number;
  budget?: number;
  protocol?: string;
  efforts?: string[];
  effort?: string;
  default_effort?: string;
  kind?: string;
  local?: boolean;
  provider?: string;
  profile_id?: string;
}
// The companion's presentation switches (backend/pebble/companion_settings.py; design study §2.5).
export interface CompanionSettings {
  pointer: boolean;
  reactions: boolean;
  status_words: "rotate" | "fixed" | "off";
  ui_guidance: boolean;
}
// Phase 46 (D166): which requests ask for OpenAI's Fast tier (backend/pebble/runner.py fast_now; settings_file.FAST).
export type FastReplies = "off" | "follow_ups" | "long_tasks" | "always";
export interface State {
  job_notifications?: { id: string; chat_id: string; title: string; created: number }[];
  chats: Chat[];
  model: ModelStatus;
  guard: { available: boolean; error: string | null };
  active: string | null;
  active_run?: string | null;
  // Phase 19: state() reports every run (app.py `runs`); the companion seeds a live or waiting run from it.
  runs?: { id: string; chat_id: string; state: string; created?: number; updated?: number }[];
  settings: {
    theme: string;
    reduced_motion: boolean;
    stealth?: boolean; // Phase 33: Stealth Lockdown
    work_view?: boolean; // Phase 46 (1.9.29, D206): Pebble's work as cards (absent: on)
    visual_answers?: "ask" | "auto"; // Phase 46 (1.9.29, D195): artifact_schema.visual_offer (absent: ask)
    memory_enabled: boolean;
    memory_layers?: Record<string, boolean>;
    permissions: Permissions;
    companion?: CompanionSettings;
    fast_replies?: FastReplies; // Phase 46 (D166): OpenAI's Fast tier on the ChatGPT sign-in
  };
  memory?: {
    enabled: boolean;
    strategy: string;
    types: { id: string; name: string; count: number; enabled?: boolean }[];
    learning?: {
      status_counts: Record<string, number>;
      last_error_type?: string | null;
    };
  };
  profiles?: ModelProfile[];
  data_dir: string;
  platform: string;
  version: string;
}
export interface Permissions {
  mode: "ask" | "auto" | "full";
  tools: {
    files: boolean;
    edits: boolean;
    tests: boolean;
    apps: boolean;
    web: boolean;
    terminal: boolean;
    plugins: boolean;
    views: boolean;
    screen?: boolean; // Phase 16: off by default
    capture?: boolean; // Phase 40 (D93): on by default; macOS Screen Recording is the person's own gate
  };
}
export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  tokens_per_second: number | null;
  route?: string;
  calls?: number;
  seconds?: number;
  generation_seconds?: number; // the runner's name for the same figure
  // Phase 22 (t22-stream): what the provider did report, summed over the attempts that reported it. The totals
  // above stay unknown (null) once an attempt that was sent reported nothing; the meter then shows these, marked.
  reported?: UsageReport | null;
  usage_incomplete?: boolean;
  unsent_attempts?: number; // attempts Pebble never sent (no tokens used); the totals stay known
}
export interface UsageReport {
  input: number | null;
  cached: number | null;
  output: number | null;
  reasoning: number | null;
  tokens_per_second: number | null;
  attempts?: number;
  generation_seconds?: number;
}
export interface ModelProfile {
  id: string;
  name: string;
  kind: string;
  path?: string;
  url?: string;
  model?: string;
  context: number;
  budget?: number;
  email?: string;
  connection_id?: string;
  provider_name?: string;
  display_name?: string;
  context_mode?: string;
  context_source?: string;
  advertised_max_context?: number;
  extended_context_available?: boolean;
  tools: boolean;
  has_key?: boolean;
  local?: boolean;
}

export interface Attachment {
  id: string;
  name: string;
  kind?: string;
  characters?: number;
  preview?: string;
  bytes?: number;
  notice?: string;
}
