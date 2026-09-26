/**
 * CallWiz AI — SDK TypeScript minimal pour le frontend (Lovable / React).
 * Zéro dépendance. Copier ce fichier dans `src/lib/callwiz-sdk.ts`.
 *
 *   const cw = new CallWizClient(import.meta.env.VITE_CALLWIZ_API_URL);
 *   await cw.login(email, password);
 *   const campaigns = await cw.campaigns.list();
 *   const stop = cw.subscribe("analytics", (e) => setStats(e.payload));
 */

export type UUID = string;
export type CampaignStatus = "draft" | "running" | "paused" | "completed" | "stopped" | "failed";
export type CallStatus = "queued" | "ringing" | "active" | "completed" | "failed" | "transferred" | "no_answer" | "busy" | "voicemail";
export type Objective = "confirm_appointment" | "qualify_lead" | "request_payment" | "collect_feedback" | "invite_event" | "reactivate_customer" | "schedule_meeting" | "custom";
export type Outcome = "success" | "failed" | "callback" | "not_interested" | "wrong_number" | "voicemail" | "transferred" | "opted_out" | "no_answer";

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface ApiErrorBody { code: string; message: string; details?: unknown }
export interface Tokens { access_token: string; refresh_token: string; token_type: "bearer"; expires_in: number }
export interface User { id: UUID; email: string; full_name: string | null; role: string; organization_id: UUID }
export interface Organization {
  id: UUID; name: string; slug: string; default_language: string; default_voice_id: string; timezone: string;
  tone: string; business_description: string; transfer_number: string | null; settings: Record<string, unknown>; created_at: string;
}
export interface CampaignSchedule { days: number[]; start: string; end: string }
export interface CampaignInput {
  name: string; objective?: Objective; objective_description?: string; script?: string; context?: string;
  voice_id?: string | null; language?: string; max_concurrency?: number; max_attempts?: number; retry_delay_minutes?: number;
  schedule?: CampaignSchedule; voicemail_behavior?: "leave_message" | "hangup"; voicemail_message?: string | null;
  knowledge_base_id?: UUID | null; from_number?: string | null; webhook_url?: string | null; allowed_tools?: string[];
  transfer_number?: string | null;
}
export interface Campaign extends Required<Omit<CampaignInput, "voice_id" | "voicemail_message" | "knowledge_base_id" | "from_number" | "webhook_url" | "transfer_number">> {
  id: UUID; organization_id: UUID; status: CampaignStatus; created_at: string; started_at: string | null; completed_at: string | null;
  voice_id: string | null; voicemail_message: string | null; knowledge_base_id: UUID | null; from_number: string | null;
  webhook_url: string | null; transfer_number: string | null;
}
export interface CampaignProgress {
  campaign_id: UUID; status: CampaignStatus; total_contacts: number; pending: number; calling: number; completed: number;
  failed: number; opted_out: number; retry: number; active_calls: number; success_count: number; percent: number;
}
export interface Contact {
  id: UUID; campaign_id: UUID | null; first_name: string | null; last_name: string | null; phone: string; email: string | null;
  attributes: Record<string, unknown>; status: string; attempts: number; last_outcome: string | null; created_at: string;
}
export interface ContactImportResult { imported: number; skipped: number; errors: { row: number; error: string }[]; detected_columns: string[]; mapping_used: Record<string, string> }
export interface TranscriptTurn { role: "user" | "assistant" | "system" | "tool"; text: string; at: number; interrupted?: boolean }
export interface Call {
  id: UUID; organization_id: UUID; campaign_id: UUID | null; contact_id: UUID | null; direction: "inbound" | "outbound";
  status: CallStatus; provider: string; from_number: string; to_number: string; started_at: string; answered_at: string | null;
  ended_at: string | null; duration_seconds: number; cost_estimate: number; outcome: Outcome | null; intent: string | null;
  sentiment: number | null; summary: string | null; created_at: string;
}
export interface CallDetail extends Call {
  transcript: TranscriptTurn[] | null; latency: { voice_to_voice_ms?: number[]; avg_ms?: number | null; p95_ms?: number | null };
  cost_breakdown: Record<string, unknown>; next_best_action: string | null; error: string | null;
}
export interface CallEvent { id: UUID; type: string; payload: Record<string, unknown>; created_at: string }
export interface AgentDecision { id: UUID; agent: string; decision: string; confidence: number; reason: string; data: Record<string, unknown>; created_at: string }
export interface CallDebug { call: CallDetail; events: CallEvent[]; decisions: AgentDecision[]; rag_chunks: Record<string, unknown>[] }
export interface PhoneNumber {
  id: UUID; organization_id: UUID; number: string; provider: "twilio" | "mock"; direction: "inbound" | "outbound" | "both";
  active: boolean; label: string | null; greeting: string | null; script: string | null; knowledge_base_id: UUID | null;
  voice_id: string | null; business_hours: Record<string, [string, string]>; allowed_tools: string[]; transfer_number: string | null; created_at: string;
}
export interface KnowledgeBase { id: UUID; organization_id: UUID; name: string; description: string; created_at: string }
export interface DocumentInfo { id: UUID; knowledge_base_id: UUID; filename: string; source_type: string; status: "pending" | "processing" | "ready" | "failed"; error: string | null; chunk_count: number; created_at: string }
export interface RetrievedChunk { chunk_id: string; document_id: string; content: string; score: number; source: string; metadata: Record<string, unknown> }
export interface TestQueryResult { query: string; chunks: RetrievedChunk[]; low_confidence: boolean; latency_ms: number }
export interface AnalyticsOverview {
  period_days: number; total_calls: number; completed_calls: number; failed_calls: number; transferred_calls: number;
  active_calls: number; queued_calls: number; average_duration_s: number; average_cost: number; total_cost: number;
  average_sentiment: number | null; conversion_rate: number; transfer_rate: number; average_voice_latency_ms: number | null;
  p95_voice_latency_ms: number | null; top_intents: { intent: string; count: number }[]; outcomes: Record<string, number>;
  calls_per_hour: { hour: string; count: number }[]; running_campaigns: number;
}
export interface RealtimeSnapshot {
  active_calls: number; active_by_direction: Record<string, number>; calls_today: number; completed_today: number;
  failed_today: number; cost_today: number; avg_latency_ms: number | null; capacity: number; utilization: number;
}

export type EventName =
  | "call.started" | "call.ringing" | "call.answered" | "call.updated" | "call.transcript_partial" | "call.transcript_final"
  | "call.response_generated" | "call.tts_started" | "call.user_interrupted" | "call.tool_called" | "call.agent_decision"
  | "call.latency" | "call.transferred" | "call.completed" | "call.failed" | "campaign.progress" | "campaign.status"
  | "analytics.updated" | "connected" | "error" | "ping" | "pong";
export interface RealtimeEvent<P = Record<string, any>> {
  id?: string; event: EventName; organization_id?: UUID; call_id?: UUID | null; campaign_id?: UUID | null; timestamp?: string; payload: P;
}

export class CallWizError extends Error {
  constructor(public status: number, public body: ApiErrorBody) { super(body.message); }
}

export class CallWizClient {
  private access: string | null = null;
  private refresh: string | null = null;

  constructor(public baseUrl: string, private storage: Storage | null = typeof localStorage !== "undefined" ? localStorage : null) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    try {
      this.access = this.storage?.getItem("cw_access") ?? null;
      this.refresh = this.storage?.getItem("cw_refresh") ?? null;
    } catch { /* stockage indisponible */ }
  }

  get isAuthenticated() { return !!this.access; }

  private setTokens(t: Tokens | null) {
    this.access = t?.access_token ?? null;
    this.refresh = t?.refresh_token ?? null;
    try {
      if (t) { this.storage?.setItem("cw_access", t.access_token); this.storage?.setItem("cw_refresh", t.refresh_token); }
      else { this.storage?.removeItem("cw_access"); this.storage?.removeItem("cw_refresh"); }
    } catch { /* ignore */ }
  }

  async request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
    const isForm = typeof FormData !== "undefined" && body instanceof FormData;
    const res = await fetch(`${this.baseUrl}/api/v1${path}`, {
      method,
      headers: { ...(isForm ? {} : { "Content-Type": "application/json" }), ...(this.access ? { Authorization: `Bearer ${this.access}` } : {}) },
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
    if (res.status === 401 && retry && this.refresh) {
      const ok = await this.tryRefresh();
      if (ok) return this.request<T>(method, path, body, false);
    }
    if (res.status === 204) return undefined as T;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new CallWizError(res.status, data.error ?? { code: "http_error", message: res.statusText });
    return data as T;
  }

  private async tryRefresh(): Promise<boolean> {
    try {
      const t = await this.request<Tokens>("POST", "/auth/refresh", { refresh_token: this.refresh }, false);
      this.setTokens(t);
      return true;
    } catch { this.setTokens(null); return false; }
  }

  async login(email: string, password: string) { this.setTokens(await this.request<Tokens>("POST", "/auth/login", { email, password }, false)); }
  async register(input: { email: string; password: string; organization_name: string; full_name?: string }) {
    this.setTokens(await this.request<Tokens>("POST", "/auth/register", input, false));
  }
  logout() { this.setTokens(null); }
  me() { return this.request<User>("GET", "/auth/me"); }

  organization = {
    get: () => this.request<Organization>("GET", "/organizations/me"),
    update: (patch: Partial<Organization>) => this.request<Organization>("PATCH", "/organizations/me", patch),
  };

  campaigns = {
    list: (params: { status?: CampaignStatus; limit?: number; offset?: number } = {}) => this.request<Page<Campaign>>("GET", `/campaigns${qs(params)}`),
    get: (id: UUID) => this.request<Campaign>("GET", `/campaigns/${id}`),
    create: (input: CampaignInput) => this.request<Campaign>("POST", "/campaigns", input),
    update: (id: UUID, patch: Partial<CampaignInput>) => this.request<Campaign>("PATCH", `/campaigns/${id}`, patch),
    remove: (id: UUID) => this.request<void>("DELETE", `/campaigns/${id}`),
    start: (id: UUID) => this.request<Campaign>("POST", `/campaigns/${id}/start`),
    pause: (id: UUID) => this.request<Campaign>("POST", `/campaigns/${id}/pause`),
    stop: (id: UUID) => this.request<Campaign>("POST", `/campaigns/${id}/stop`),
    progress: (id: UUID) => this.request<CampaignProgress>("GET", `/campaigns/${id}/progress`),
    contacts: (id: UUID, params: { status?: string; limit?: number; offset?: number } = {}) => this.request<Page<Contact>>("GET", `/campaigns/${id}/contacts${qs(params)}`),
    importCsv: (id: UUID, file: File | Blob, mapping?: Record<string, string>) => {
      const fd = new FormData();
      fd.append("file", file);
      if (mapping) fd.append("mapping", JSON.stringify(mapping));
      return this.request<ContactImportResult>("POST", `/campaigns/${id}/contacts/import`, fd);
    },
    analytics: (id: UUID) => this.request<Record<string, any>>("GET", `/analytics/campaigns/${id}`),
  };

  calls = {
    list: (params: { status?: CallStatus; direction?: string; campaign_id?: UUID; limit?: number; offset?: number } = {}) => this.request<Page<Call>>("GET", `/calls${qs(params)}`),
    get: (id: UUID) => this.request<CallDetail>("GET", `/calls/${id}`),
    transcript: (id: UUID) => this.request<{ call_id: UUID; transcript: TranscriptTurn[]; summary: string | null }>("GET", `/calls/${id}/transcript`),
    events: (id: UUID) => this.request<CallEvent[]>("GET", `/calls/${id}/events`),
    debug: (id: UUID) => this.request<CallDebug>("GET", `/calls/${id}/debug`),
    hangup: (id: UUID) => this.request<{ ended: boolean }>("POST", `/calls/${id}/hangup`),
    simulate: (input: { direction?: "inbound" | "outbound"; campaign_id?: UUID; phone_number_id?: UUID; caller_script?: string[] }) => this.request<Call>("POST", "/calls/simulate", input),
    testOutbound: (to: string, campaign_id?: UUID) => this.request<Call>("POST", "/calls/test-outbound", { to, campaign_id }),
  };

  phoneNumbers = {
    list: () => this.request<PhoneNumber[]>("GET", "/phone-numbers"),
    create: (input: Partial<PhoneNumber> & { number: string }) => this.request<PhoneNumber>("POST", "/phone-numbers", input),
    update: (id: UUID, patch: Partial<PhoneNumber>) => this.request<PhoneNumber>("PATCH", `/phone-numbers/${id}`, patch),
    remove: (id: UUID) => this.request<void>("DELETE", `/phone-numbers/${id}`),
  };

  knowledge = {
    list: () => this.request<KnowledgeBase[]>("GET", "/knowledge-bases"),
    create: (name: string, description = "") => this.request<KnowledgeBase>("POST", "/knowledge-bases", { name, description }),
    get: (id: UUID) => this.request<KnowledgeBase & { documents: DocumentInfo[] }>("GET", `/knowledge-bases/${id}`),
    upload: (id: UUID, file: File | Blob, filename?: string) => {
      const fd = new FormData();
      fd.append("file", file, filename);
      return this.request<DocumentInfo>("POST", `/knowledge-bases/${id}/documents`, fd);
    },
    addText: (id: UUID, content: string, filename = "note.md") => this.request<DocumentInfo>("POST", `/knowledge-bases/${id}/documents/text`, { content, filename }),
    documentStatus: (docId: UUID) => this.request<DocumentInfo>("GET", `/documents/${docId}/status`),
    removeDocument: (docId: UUID) => this.request<void>("DELETE", `/documents/${docId}`),
    testQuery: (id: UUID, query: string, top_k = 4) => this.request<TestQueryResult>("POST", `/knowledge-bases/${id}/test-query`, { query, top_k }),
  };

  analytics = {
    overview: (days = 7) => this.request<AnalyticsOverview>("GET", `/analytics/overview?days=${days}`),
    realtime: () => this.request<RealtimeSnapshot>("GET", "/analytics/realtime"),
    timeseries: (days = 7, bucket: "hour" | "day" = "hour") => this.request<{ bucket: string; series: Record<string, number | string>[] }>("GET", `/analytics/calls?days=${days}&bucket=${bucket}`),
  };

  /** Abonnement WebSocket avec reconnexion automatique (backoff). Retourne une fonction de désabonnement. */
  subscribe(channel: "calls" | "analytics" | { campaign: UUID }, onEvent: (e: RealtimeEvent) => void, onStatus?: (s: "open" | "closed") => void): () => void {
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    const path = typeof channel === "string" ? `/ws/${channel}` : `/ws/campaigns/${channel.campaign}`;
    const connect = () => {
      if (closed || !this.access) return;
      ws = new WebSocket(`${this.baseUrl.replace(/^http/, "ws")}${path}?token=${encodeURIComponent(this.access)}`);
      ws.onopen = () => { attempt = 0; onStatus?.("open"); };
      ws.onmessage = (m) => {
        const e = JSON.parse(m.data) as RealtimeEvent;
        if (e.event === "ping" || e.event === "pong") return;
        onEvent(e);
      };
      ws.onclose = async (ev) => {
        onStatus?.("closed");
        if (closed) return;
        if (ev.code === 4401) await this.tryRefresh();
        setTimeout(connect, Math.min(15000, 500 * 2 ** attempt++));
      };
    };
    connect();
    return () => { closed = true; ws?.close(); };
  }
}

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)]))}` : "";
}
