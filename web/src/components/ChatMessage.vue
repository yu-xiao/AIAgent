<script setup lang="ts">
defineProps<{
  role: "user" | "assistant" | "system";
  content: string;
  pending?: boolean;
}>();
</script>

<template>
  <article class="message" :class="`message--${role}`">
    <div class="message__avatar" aria-hidden="true">{{ role === "user" ? "你" : "AI" }}</div>
    <div class="message__body">
      <span class="message__author">{{ role === "user" ? "你" : "企业助手" }}</span>
      <div v-if="content" class="message__content">{{ content }}</div>
      <div v-else-if="pending" class="thinking" aria-label="Agent 正在思考">
        <span />
        <span />
        <span />
      </div>
    </div>
  </article>
</template>
