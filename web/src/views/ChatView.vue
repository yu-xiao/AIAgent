<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import { ApiError } from "@/api/client";
import {
  cancelRun,
  createConversation,
  createMessageRun,
  getConversation,
  getRun,
  listConversationRuns,
  listConversations,
} from "@/api/client";
import { streamRunEvents } from "@/api/sse";
import type { Conversation, Message, Run, RunEvent, RunStatus } from "@/api/types";
import ChatComposer from "@/components/ChatComposer.vue";
import ChatMessage from "@/components/ChatMessage.vue";
import ConversationSidebar from "@/components/ConversationSidebar.vue";
import { useSession } from "@/composables/useSession";

const TERMINAL_STATUSES = new Set<RunStatus>([
  "completed",
  "failed",
  "cancelled",
  "timed_out",
]);

const session = useSession();
const conversations = ref<Conversation[]>([]);
const selectedConversationId = ref<string | null>(null);
const selectedConversation = computed(() =>
  conversations.value.find((item) => item.id === selectedConversationId.value),
);
const messages = ref<Message[]>([]);
const loadingConversations = ref(true);
const loadingConversation = ref(false);
const creatingConversation = ref(false);
const sending = ref(false);
const activeRun = ref<Run | null>(null);
const streamedAnswer = ref("");
const runStage = ref("");
const runError = ref<string | null>(null);
const runTraceId = ref<string | null>(null);
const messagesEnd = ref<HTMLElement | null>(null);
let streamController: AbortController | null = null;
let selectionVersion = 0;

const organizationId = computed(() => session.state.organizationId);
const csrfToken = computed(() => session.state.user?.csrf_token ?? "");
const isBusy = computed(
  () => sending.value || (activeRun.value !== null && !TERMINAL_STATUSES.has(activeRun.value.status)),
);

watch(
  organizationId,
  (value) => {
    if (value) void initializeOrganization(value);
  },
  { immediate: true },
);

watch(
  () => [messages.value.length, streamedAnswer.value],
  () => void scrollToBottom(),
);

onBeforeUnmount(stopStream);

async function initializeOrganization(currentOrganizationId: string): Promise<void> {
  ++selectionVersion;
  stopStream();
  loadingConversations.value = true;
  selectedConversationId.value = null;
  messages.value = [];
  activeRun.value = null;
  streamedAnswer.value = "";
  runError.value = null;
  try {
    const items = await listConversations(currentOrganizationId);
    if (organizationId.value !== currentOrganizationId) return;
    conversations.value = items;
    const firstConversation = items[0];
    if (firstConversation) await openConversation(firstConversation.id);
  } catch (error) {
    if (organizationId.value === currentOrganizationId) runError.value = friendlyError(error);
  } finally {
    if (organizationId.value === currentOrganizationId) loadingConversations.value = false;
  }
}

async function refreshConversationList(): Promise<void> {
  if (!organizationId.value) return;
  conversations.value = await listConversations(organizationId.value);
}

async function openConversation(conversationId: string): Promise<void> {
  if (!organizationId.value) return;
  const version = ++selectionVersion;
  stopStream();
  selectedConversationId.value = conversationId;
  loadingConversation.value = true;
  messages.value = [];
  activeRun.value = null;
  streamedAnswer.value = "";
  runStage.value = "";
  runError.value = null;
  runTraceId.value = null;
  try {
    const [conversation, recentRuns] = await Promise.all([
      getConversation(organizationId.value, conversationId),
      listConversationRuns(organizationId.value, conversationId, 1),
    ]);
    if (version !== selectionVersion) return;
    messages.value = conversation.messages;
    const latestRun = recentRuns[0];
    if (latestRun && !TERMINAL_STATUSES.has(latestRun.status)) {
      void followRun(latestRun);
    }
  } catch (error) {
    if (version === selectionVersion) runError.value = friendlyError(error);
  } finally {
    if (version === selectionVersion) loadingConversation.value = false;
  }
}

async function startNewConversation(): Promise<void> {
  if (!organizationId.value || !csrfToken.value || creatingConversation.value) return;
  creatingConversation.value = true;
  runError.value = null;
  try {
    const conversation = await createConversation(
      organizationId.value,
      csrfToken.value,
      "新对话",
    );
    conversations.value = [conversation, ...conversations.value];
    await openConversation(conversation.id);
  } catch (error) {
    runError.value = friendlyError(error);
  } finally {
    creatingConversation.value = false;
  }
}

async function sendMessage(content: string): Promise<void> {
  if (!organizationId.value || !csrfToken.value || isBusy.value) return;
  sending.value = true;
  runError.value = null;
  let conversationId = selectedConversationId.value;
  try {
    if (!conversationId) {
      const conversation = await createConversation(
        organizationId.value,
        csrfToken.value,
        conversationTitle(content),
      );
      conversations.value = [conversation, ...conversations.value];
      selectedConversationId.value = conversation.id;
      conversationId = conversation.id;
    }
    const optimisticMessage: Message = {
      id: `pending-${crypto.randomUUID()}`,
      role: "user",
      content,
      created_at: new Date().toISOString(),
    };
    messages.value.push(optimisticMessage);
    streamedAnswer.value = "";
    runStage.value = "正在提交问题…";
    const run = await createMessageRun(
      organizationId.value,
      csrfToken.value,
      conversationId,
      content,
    );
    await refreshConversationList();
    void followRun(run);
  } catch (error) {
    runError.value = friendlyError(error);
    if (conversationId) {
      try {
        messages.value = (await getConversation(organizationId.value, conversationId)).messages;
      } catch {
        // Preserve the actionable request error if refreshing the conversation also fails.
      }
    }
  } finally {
    sending.value = false;
  }
}

async function followRun(run: Run): Promise<void> {
  stopStream();
  const controller = new AbortController();
  streamController = controller;
  activeRun.value = run;
  runTraceId.value = run.trace_id;
  streamedAnswer.value = "";
  runStage.value = run.status === "queued" ? "等待 Agent 开始…" : "Agent 正在生成回答…";
  runError.value = null;
  try {
    await streamRunEvents({
      path: run.events_url,
      organizationId: organizationId.value ?? "",
      signal: controller.signal,
      onEvent: handleRunEvent,
    });
    if (!controller.signal.aborted) await synchronizeRun(run.id);
  } catch (error) {
    if (!controller.signal.aborted) {
      runError.value = `实时连接已中断。${friendlyError(error)}`;
      runStage.value = "";
    }
  } finally {
    if (streamController === controller) streamController = null;
  }
}

function handleRunEvent(event: RunEvent): void {
  if (event.type === "message.delta" && typeof event.data.delta === "string") {
    streamedAnswer.value += event.data.delta;
    runStage.value = "Agent 正在生成回答…";
  } else if (event.type === "run.started") {
    if (activeRun.value) activeRun.value.status = "running";
    runStage.value = "Agent 正在理解你的问题…";
  } else if (event.type === "run.tool_invocation") {
    const toolName = typeof event.data.tool_name === "string" ? event.data.tool_name : "业务工具";
    runStage.value = `已查询 ${toolName}`;
  } else if (event.type === "run.citation") {
    runStage.value = "正在整理来源…";
  } else if (event.type === "run.cancellation_requested") {
    runStage.value = "正在停止…";
  } else if (event.type === "run.snapshot" && typeof event.data.status === "string") {
    updateRunStatus(event.data.status as RunStatus);
  } else if (event.type.startsWith("run.")) {
    updateRunStatus(event.type.slice(4) as RunStatus);
  }
}

function updateRunStatus(status: RunStatus): void {
  if (activeRun.value) activeRun.value.status = status;
  if (status === "completed") runStage.value = "回答已完成";
  else if (status === "cancelled") runStage.value = "本次生成已停止";
  else if (status === "timed_out") runStage.value = "本次生成超时";
  else if (status === "failed") runStage.value = "本次生成失败";
}

async function synchronizeRun(runId: string): Promise<void> {
  if (!organizationId.value || !selectedConversationId.value) return;
  const finalRun = await getRun(organizationId.value, runId);
  runTraceId.value = finalRun.trace_id;
  if (!TERMINAL_STATUSES.has(finalRun.status)) {
    activeRun.value = finalRun;
    throw new Error("运行仍在继续，你可以重新连接实时结果。 ");
  }
  activeRun.value = null;
  const conversation = await getConversation(
    organizationId.value,
    selectedConversationId.value,
  );
  messages.value = conversation.messages;
  streamedAnswer.value = "";
  if (finalRun.status === "failed" || finalRun.status === "timed_out") {
    runError.value = finalRun.error_message ?? statusLabel(finalRun.status);
  }
  await refreshConversationList();
}

async function stopRun(): Promise<void> {
  if (!organizationId.value || !csrfToken.value || !activeRun.value) return;
  runStage.value = "正在停止…";
  try {
    activeRun.value = await cancelRun(
      organizationId.value,
      csrfToken.value,
      activeRun.value.id,
    );
  } catch (error) {
    runError.value = friendlyError(error);
  }
}

function reconnectRun(): void {
  if (activeRun.value) void followRun(activeRun.value);
}

function stopStream(): void {
  streamController?.abort();
  streamController = null;
}

async function scrollToBottom(): Promise<void> {
  await nextTick();
  messagesEnd.value?.scrollIntoView({ behavior: "smooth", block: "end" });
}

function conversationTitle(content: string): string {
  const normalized = content.replace(/\s+/g, " ").trim();
  return normalized.length > 28 ? `${normalized.slice(0, 28)}…` : normalized;
}

function friendlyError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录状态已失效，请重新登录。";
    if (error.code === "access_denied") return "当前账号没有执行此操作的权限。";
    if (error.code === "conflict") return "当前对话已有任务在运行，请刷新后继续。";
    if (error.code === "model_provider_error") return "模型服务暂时不可用，请稍后重试。";
    if (error.code.startsWith("mcp_")) return "业务系统查询暂时失败，请稍后重试。";
    return error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") return "连接已停止。";
  return error instanceof Error ? error.message : "操作未能完成，请重试。";
}

function statusLabel(status: RunStatus): string {
  const labels: Record<RunStatus, string> = {
    queued: "等待执行",
    running: "正在生成",
    completed: "回答已完成",
    failed: "生成失败",
    cancelled: "已停止",
    timed_out: "生成超时",
  };
  return labels[status];
}
</script>

<template>
  <div class="chat-layout">
    <ConversationSidebar
      :conversations="conversations"
      :selected-id="selectedConversationId"
      :loading="loadingConversations"
      :creating="creatingConversation"
      @select="openConversation"
      @create="startNewConversation"
    />

    <main class="chat-main">
      <header class="chat-header">
        <div>
          <span class="eyebrow">CONVERSATION</span>
          <h2>{{ selectedConversation?.title || "新的对话" }}</h2>
        </div>
        <span class="trust-badge"><i /> 安全连接 · 来源可追溯</span>
      </header>

      <section class="message-feed" aria-live="polite" :aria-busy="loadingConversation">
        <div v-if="loadingConversation" class="message-feed__loading">
          <span class="spinner" />
          <span>正在加载对话…</span>
        </div>

        <div v-else-if="messages.length === 0 && !activeRun" class="chat-welcome">
          <span class="chat-welcome__mark">✦</span>
          <h2>今天想了解什么？</h2>
          <p>我可以结合已授权的企业系统，为你查询并解释业务信息。</p>
          <div class="prompt-grid">
            <button type="button" @click="sendMessage('我可以访问哪些数据集？')">
              <span>权限概览</span>
              我可以访问哪些数据集？
            </button>
            <button type="button" @click="sendMessage('请解释我的当前有效权限。')">
              <span>权限解释</span>
              请解释我的当前有效权限
            </button>
            <button type="button" @click="sendMessage('如何开始使用企业 AI Agent？')">
              <span>使用帮助</span>
              如何开始使用企业 AI Agent？
            </button>
          </div>
        </div>

        <div v-else class="message-list">
          <ChatMessage
            v-for="message in messages"
            :key="message.id"
            :role="message.role"
            :content="message.content"
          />
          <ChatMessage
            v-if="activeRun"
            role="assistant"
            :content="streamedAnswer"
            :pending="!streamedAnswer"
          />
          <div ref="messagesEnd" />
        </div>
      </section>

      <div v-if="runStage && (activeRun || runStage.includes('停止'))" class="run-status">
        <span v-if="activeRun" class="run-status__pulse" />
        <span>{{ runStage }}</span>
        <small v-if="runTraceId">Trace {{ runTraceId.slice(0, 8) }}</small>
      </div>

      <div v-if="runError" class="chat-error" role="alert">
        <span>!</span>
        <p>{{ runError }}</p>
        <button v-if="activeRun" type="button" @click="reconnectRun">重新连接</button>
        <button v-else type="button" @click="runError = null">关闭</button>
      </div>

      <ChatComposer :busy="isBusy" :disabled="loadingConversation" @send="sendMessage" @cancel="stopRun" />
    </main>
  </div>
</template>
