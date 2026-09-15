<script setup lang="ts">
import { computed, ref } from "vue";

import { localLogin, localRegister, loginUrl } from "@/api/client";
import type { AuthOptions } from "@/api/types";

const props = defineProps<{ authOptions: AuthOptions | null; error: string | null }>();
const emit = defineEmits<{ authenticated: []; retry: [] }>();

const mode = ref<"login" | "register">("login");
const email = ref("");
const displayName = ref("");
const password = ref("");
const submitting = ref(false);
const formError = ref<string | null>(null);

const isRegistration = computed(() => mode.value === "register");

async function submit(): Promise<void> {
  if (!props.authOptions?.local_enabled || submitting.value) return;
  formError.value = null;
  submitting.value = true;
  try {
    if (isRegistration.value) {
      await localRegister({
        email: email.value,
        display_name: displayName.value,
        password: password.value,
      });
    } else {
      await localLogin({ email: email.value, password: password.value });
    }
    password.value = "";
    emit("authenticated");
  } catch (error) {
    formError.value = error instanceof Error ? error.message : "登录失败，请重试。";
  } finally {
    submitting.value = false;
  }
}

function switchMode(nextMode: "login" | "register"): void {
  mode.value = nextMode;
  password.value = "";
  formError.value = null;
}

function signIn(): void {
  window.location.assign(loginUrl());
}
</script>

<template>
  <main class="login-page">
    <section class="login-hero">
      <div class="login-hero__brand">
        <span class="brand-mark">AI</span>
        <span>企业 AI Agent</span>
      </div>
      <div class="login-hero__content">
        <span class="eyebrow eyebrow--light">TRUSTED BUSINESS INTELLIGENCE</span>
        <h1>让企业数据<br />真正参与每一次决策</h1>
        <p>连接你的业务账号，用自然语言获得安全、可追溯的答案。</p>
      </div>
      <div class="login-hero__proof">
        <div><strong>独立身份</strong><span>平台账号与业务账号隔离</span></div>
        <div><strong>权限继承</strong><span>查询遵循你的真实业务权限</span></div>
        <div><strong>来源可溯</strong><span>关键事实保留查询来源</span></div>
      </div>
    </section>

    <section class="login-panel">
      <div class="login-card">
        <span class="login-card__kicker">{{ isRegistration ? "快速开始" : "欢迎回来" }}</span>
        <h2>{{ isRegistration ? "创建本地账号" : "登录工作台" }}</h2>
        <p>
          {{
            isRegistration
              ? "注册后会自动创建个人工作区，立即开始使用。"
              : "登录后继续访问你的对话与工作区。"
          }}
        </p>
        <div v-if="error" class="alert alert--error" role="alert">
          <div>
            <strong>暂时无法连接服务</strong>
            <span>{{ error }}</span>
          </div>
          <button type="button" @click="$emit('retry')">重试</button>
        </div>
        <form v-if="authOptions?.local_enabled" class="login-form" @submit.prevent="submit">
          <label v-if="isRegistration">
            <span>显示名称</span>
            <input
              v-model="displayName"
              autocomplete="name"
              maxlength="100"
              placeholder="例如：张三"
              required
            />
          </label>
          <label>
            <span>邮箱</span>
            <input
              v-model="email"
              autocomplete="email"
              maxlength="320"
              placeholder="name@example.com"
              required
              type="email"
            />
          </label>
          <label>
            <span>密码</span>
            <input
              v-model="password"
              :autocomplete="isRegistration ? 'new-password' : 'current-password'"
              :minlength="isRegistration ? 12 : 1"
              maxlength="128"
              :placeholder="isRegistration ? '至少 12 位，避免简单重复' : '输入密码'"
              required
              type="password"
            />
          </label>
          <div v-if="formError" class="alert alert--error" role="alert">
            <span>{{ formError }}</span>
          </div>
          <button class="button button--primary button--wide" :disabled="submitting" type="submit">
            {{ submitting ? "正在处理…" : isRegistration ? "注册并进入工作台" : "登录" }}
            <span v-if="!submitting" aria-hidden="true">→</span>
          </button>
          <button
            v-if="authOptions.registration_enabled"
            class="login-form__switch"
            type="button"
            @click="switchMode(isRegistration ? 'login' : 'register')"
          >
            {{ isRegistration ? "已有账号？返回登录" : "没有账号？立即注册" }}
          </button>
        </form>
        <div v-if="authOptions?.oidc_enabled && authOptions.local_enabled" class="login-divider">
          <span>或</span>
        </div>
        <button
          v-if="authOptions?.oidc_enabled"
          class="button button--secondary button--wide"
          type="button"
          @click="signIn"
        >
          使用企业统一身份登录
          <span aria-hidden="true">→</span>
        </button>
        <div
          v-if="authOptions && !authOptions.local_enabled && !authOptions.oidc_enabled"
          class="alert alert--error"
          role="alert"
        >
          <span>平台尚未配置可用的登录方式，请联系管理员。</span>
        </div>
        <small>本地账号仅用于开发和内部测试，请勿复用其他系统的密码。</small>
      </div>
    </section>
  </main>
</template>
