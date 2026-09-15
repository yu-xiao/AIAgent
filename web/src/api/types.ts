export interface Organization {
  id: string;
  name: string;
  created_at: string;
}

export interface CurrentUser {
  id: string;
  display_name: string;
  email: string | null;
  csrf_token: string;
  organizations: Organization[];
}

export interface AuthOptions {
  local_enabled: boolean;
  registration_enabled: boolean;
  oidc_enabled: boolean;
}

export interface LocalLoginInput {
  email: string;
  password: string;
}

export interface LocalRegisterInput extends LocalLoginInput {
  display_name: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out";

export interface Citation {
  id: string;
  source_system: string;
  server_code: string;
  tool_name: string;
  resource_id: string | null;
  queried_at: string;
  trace_id: string;
  partial: boolean;
  created_at: string;
}

export interface Run {
  id: string;
  conversation_id: string;
  status: RunStatus;
  trace_id: string;
  agent_id: string | null;
  agent_version_id: string | null;
  agent_config_digest: string | null;
  error_code: string | null;
  error_message: string | null;
  cancellation_requested_at: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  events_url: string;
  citations: Citation[];
}

export interface RunEvent<T = Record<string, unknown>> {
  id?: string;
  type: string;
  data: T;
}

export interface ApiErrorPayload {
  error?: {
    code?: string;
    message?: string;
  };
  detail?: string | Array<{ msg?: string }>;
}
