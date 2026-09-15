<script setup lang="ts">
import { onMounted, ref } from "vue";

import { getAuthOptions } from "@/api/client";
import type { AuthOptions } from "@/api/types";
import AppShell from "@/components/AppShell.vue";
import LoginView from "@/views/LoginView.vue";
import { useSession } from "@/composables/useSession";

const session = useSession();
const authOptions = ref<AuthOptions | null>(null);
const authError = ref<string | null>(null);
const booting = ref(true);

onMounted(() => {
  void initialize();
});

async function initialize(): Promise<void> {
  booting.value = true;
  authError.value = null;
  await Promise.all([session.initialize(), loadAuthOptions()]);
  booting.value = false;
}

async function loadAuthOptions(): Promise<void> {
  try {
    authOptions.value = await getAuthOptions();
  } catch (error) {
    authOptions.value = null;
    authError.value = error instanceof Error ? error.message : "无法读取登录配置。";
  }
}

async function authenticated(): Promise<void> {
  await session.initialize();
}
</script>

<template>
  <main v-if="booting || session.state.loading" class="boot-screen" aria-live="polite">
    <div class="brand-mark brand-mark--large" aria-hidden="true">AI</div>
    <div class="boot-screen__copy">
      <strong>企业 AI Agent</strong>
      <span>正在恢复你的工作区…</span>
    </div>
  </main>
  <LoginView
    v-else-if="!session.state.authenticated"
    :auth-options="authOptions"
    :error="session.state.error ?? authError"
    @authenticated="authenticated"
    @retry="initialize"
  />
  <AppShell v-else />
</template>
