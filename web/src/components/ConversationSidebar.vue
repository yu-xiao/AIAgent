<script setup lang="ts">
import type { Conversation } from "@/api/types";

defineProps<{
  conversations: Conversation[];
  selectedId: string | null;
  loading: boolean;
  creating: boolean;
}>();

defineEmits<{
  select: [conversationId: string];
  create: [];
}>();

function formatTime(value: string): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}
</script>

<template>
  <aside class="conversation-sidebar">
    <div class="conversation-sidebar__header">
      <div>
        <span class="eyebrow">WORKSPACE</span>
        <h1>对话</h1>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label="新建对话"
        :disabled="creating"
        @click="$emit('create')"
      >
        ＋
      </button>
    </div>

    <div v-if="loading" class="conversation-list" aria-label="正在加载对话">
      <div v-for="index in 4" :key="index" class="conversation-skeleton" />
    </div>
    <div v-else-if="conversations.length === 0" class="conversation-sidebar__empty">
      <span>还没有对话</span>
      <small>提出第一个问题即可开始</small>
    </div>
    <nav v-else class="conversation-list" aria-label="历史对话">
      <button
        v-for="conversation in conversations"
        :key="conversation.id"
        type="button"
        class="conversation-item"
        :class="{ 'conversation-item--active': conversation.id === selectedId }"
        @click="$emit('select', conversation.id)"
      >
        <span class="conversation-item__title">{{ conversation.title }}</span>
        <time :datetime="conversation.updated_at">{{ formatTime(conversation.updated_at) }}</time>
      </button>
    </nav>
  </aside>
</template>
