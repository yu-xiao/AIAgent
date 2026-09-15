import { computed, reactive, readonly } from "vue";

import { ApiError, getMe, logout as logoutRequest } from "@/api/client";
import type { CurrentUser } from "@/api/types";

const ORGANIZATION_STORAGE_KEY = "ai-agent.organization-id";

interface SessionState {
  loading: boolean;
  authenticated: boolean;
  user: CurrentUser | null;
  organizationId: string | null;
  error: string | null;
}

const state = reactive<SessionState>({
  loading: true,
  authenticated: false,
  user: null,
  organizationId: null,
  error: null,
});

const currentOrganization = computed(() =>
  state.user?.organizations.find((item) => item.id === state.organizationId),
);

export function useSession() {
  async function initialize(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      const user = await getMe();
      state.user = user;
      state.authenticated = true;
      const storedOrganization = window.localStorage.getItem(ORGANIZATION_STORAGE_KEY);
      const selected = user.organizations.find((item) => item.id === storedOrganization);
      state.organizationId = selected?.id ?? user.organizations[0]?.id ?? null;
      persistOrganization(state.organizationId);
    } catch (error) {
      state.user = null;
      state.organizationId = null;
      state.authenticated = false;
      if (!(error instanceof ApiError && error.status === 401)) {
        state.error = error instanceof Error ? error.message : "无法连接到服务。";
      }
    } finally {
      state.loading = false;
    }
  }

  function selectOrganization(organizationId: string): void {
    if (!state.user?.organizations.some((item) => item.id === organizationId)) return;
    state.organizationId = organizationId;
    persistOrganization(organizationId);
  }

  async function logout(): Promise<void> {
    if (!state.user) return;
    try {
      await logoutRequest(state.user.csrf_token);
    } finally {
      state.user = null;
      state.organizationId = null;
      state.authenticated = false;
      window.localStorage.removeItem(ORGANIZATION_STORAGE_KEY);
    }
  }

  return {
    state: readonly(state),
    currentOrganization,
    initialize,
    selectOrganization,
    logout,
  };
}

function persistOrganization(organizationId: string | null): void {
  if (organizationId) window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, organizationId);
  else window.localStorage.removeItem(ORGANIZATION_STORAGE_KEY);
}
