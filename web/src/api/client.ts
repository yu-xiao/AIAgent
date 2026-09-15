import type {
  ApiErrorPayload,
  AuthOptions,
  Conversation,
  ConversationDetail,
  CurrentUser,
  LocalLoginInput,
  LocalRegisterInput,
  Run,
} from "./types";

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const API_BASE_URL = configuredBaseUrl.replace(/\/$/, "");

interface RequestOptions extends RequestInit {
  organizationId?: string;
  csrfToken?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly traceId: string | null;

  constructor(status: number, code: string, message: string, traceId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (options.organizationId) {
    headers.set("X-Organization-Id", options.organizationId);
  }
  if (options.csrfToken) {
    headers.set("X-CSRF-Token", options.csrfToken);
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    throw await createApiError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function createApiError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload = {};
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    // A proxy or upstream may return a non-JSON error page. Keep the UI message generic.
  }
  const validationMessage = Array.isArray(payload.detail)
    ? payload.detail.find((item) => item.msg)?.msg
    : payload.detail;
  const message =
    payload.error?.message ?? validationMessage ?? defaultErrorMessage(response.status);
  return new ApiError(
    response.status,
    payload.error?.code ?? `http_${response.status}`,
    message,
    response.headers.get("X-Trace-Id"),
  );
}

export function getMe(): Promise<CurrentUser> {
  return apiRequest<CurrentUser>("/api/v1/me");
}

export function getAuthOptions(): Promise<AuthOptions> {
  return apiRequest<AuthOptions>("/api/v1/auth/options");
}

export function localLogin(payload: LocalLoginInput): Promise<void> {
  return apiRequest<void>("/api/v1/auth/local/login", {
    method: "POST",
    headers: { "X-Requested-With": "ai-agent-web" },
    body: JSON.stringify(payload),
  });
}

export function localRegister(payload: LocalRegisterInput): Promise<void> {
  return apiRequest<void>("/api/v1/auth/register", {
    method: "POST",
    headers: { "X-Requested-With": "ai-agent-web" },
    body: JSON.stringify(payload),
  });
}

export function logout(csrfToken: string): Promise<void> {
  return apiRequest<void>("/api/v1/auth/logout", { method: "POST", csrfToken });
}

export function listConversations(organizationId: string): Promise<Conversation[]> {
  return apiRequest<Conversation[]>("/api/v1/conversations?limit=100", { organizationId });
}

export function createConversation(
  organizationId: string,
  csrfToken: string,
  title: string,
): Promise<Conversation> {
  return apiRequest<Conversation>("/api/v1/conversations", {
    method: "POST",
    organizationId,
    csrfToken,
    body: JSON.stringify({ title }),
  });
}

export function getConversation(
  organizationId: string,
  conversationId: string,
): Promise<ConversationDetail> {
  return apiRequest<ConversationDetail>(`/api/v1/conversations/${conversationId}`, {
    organizationId,
  });
}

export function listConversationRuns(
  organizationId: string,
  conversationId: string,
  limit = 1,
): Promise<Run[]> {
  return apiRequest<Run[]>(
    `/api/v1/conversations/${conversationId}/runs?limit=${limit}`,
    { organizationId },
  );
}

export function createMessageRun(
  organizationId: string,
  csrfToken: string,
  conversationId: string,
  content: string,
): Promise<Run> {
  return apiRequest<Run>(`/api/v1/conversations/${conversationId}/messages`, {
    method: "POST",
    organizationId,
    csrfToken,
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ content }),
  });
}

export function getRun(organizationId: string, runId: string): Promise<Run> {
  return apiRequest<Run>(`/api/v1/runs/${runId}`, { organizationId });
}

export function cancelRun(
  organizationId: string,
  csrfToken: string,
  runId: string,
): Promise<Run> {
  return apiRequest<Run>(`/api/v1/runs/${runId}/cancel`, {
    method: "POST",
    organizationId,
    csrfToken,
  });
}

export function loginUrl(): string {
  return `${API_BASE_URL}/api/v1/auth/login`;
}

function defaultErrorMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录。";
  if (status === 403) return "你没有执行此操作的权限。";
  if (status === 404) return "请求的内容不存在或已不可访问。";
  if (status === 409) return "当前操作与已有状态冲突，请刷新后重试。";
  if (status === 429) return "请求过于频繁，请稍后再试。";
  if (status >= 500) return "服务暂时不可用，请稍后再试。";
  return "请求未能完成，请重试。";
}
