<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";

import { useSession } from "@/composables/useSession";

const session = useSession();
const loggingOut = ref(false);
const initials = computed(() => session.state.user?.display_name.trim().slice(0, 1) || "用");

function changeOrganization(event: Event): void {
  session.selectOrganization((event.target as HTMLSelectElement).value);
}

async function signOut(): Promise<void> {
  loggingOut.value = true;
  try {
    await session.logout();
  } finally {
    loggingOut.value = false;
  }
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <RouterLink class="topbar__brand" to="/" aria-label="返回 AI Agent 对话">
        <span class="brand-mark">AI</span>
        <span>
          <strong>企业 AI Agent</strong>
          <small>可信业务助手</small>
        </span>
      </RouterLink>

      <div class="topbar__actions">
        <label class="organization-picker">
          <span>当前组织</span>
          <select
            :value="session.state.organizationId ?? ''"
            aria-label="切换当前组织"
            @change="changeOrganization"
          >
            <option
              v-for="organization in session.state.user?.organizations"
              :key="organization.id"
              :value="organization.id"
            >
              {{ organization.name }}
            </option>
          </select>
        </label>
        <div class="user-menu">
          <span class="user-menu__avatar" aria-hidden="true">{{ initials }}</span>
          <span class="user-menu__identity">
            <strong>{{ session.state.user?.display_name }}</strong>
            <small>{{ session.state.user?.email || "企业用户" }}</small>
          </span>
          <button class="button button--ghost button--small" :disabled="loggingOut" @click="signOut">
            {{ loggingOut ? "退出中" : "退出" }}
          </button>
        </div>
      </div>
    </header>

    <aside class="primary-nav" aria-label="主导航">
      <RouterLink to="/" class="primary-nav__item">
        <span class="primary-nav__icon" aria-hidden="true">✦</span>
        <span>对话</span>
      </RouterLink>
      <RouterLink to="/connections" class="primary-nav__item">
        <span class="primary-nav__icon" aria-hidden="true">↗</span>
        <span>连接</span>
      </RouterLink>
      <RouterLink to="/agents" class="primary-nav__item">
        <span class="primary-nav__icon" aria-hidden="true">◇</span>
        <span>Agent</span>
      </RouterLink>
    </aside>

    <section class="workspace">
      <div v-if="!session.state.organizationId" class="empty-page">
        <span class="empty-page__icon">◎</span>
        <h1>还没有可用组织</h1>
        <p>请联系管理员将你加入一个组织后再使用工作台。</p>
      </div>
      <RouterView v-else :key="session.state.organizationId" />
    </section>
  </div>
</template>
