<script setup lang="ts">
import { computed, ref } from "vue";

const props = defineProps<{
  busy: boolean;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  send: [content: string];
  cancel: [];
}>();

const content = ref("");
const canSend = computed(
  () => !props.busy && !props.disabled && content.value.trim().length > 0,
);

function submit(): void {
  const value = content.value.trim();
  if (!value || !canSend.value) return;
  content.value = "";
  emit("send", value);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
}
</script>

<template>
  <div class="composer-wrap">
    <div class="composer" :class="{ 'composer--busy': busy }">
      <textarea
        v-model="content"
        rows="1"
        maxlength="4000"
        :disabled="disabled"
        aria-label="输入问题"
        placeholder="输入你的问题，按 Enter 发送…"
        @keydown="handleKeydown"
      />
      <button
        v-if="busy"
        type="button"
        class="composer__submit composer__submit--stop"
        aria-label="停止生成"
        @click="emit('cancel')"
      >
        <span />
      </button>
      <button
        v-else
        type="button"
        class="composer__submit"
        aria-label="发送问题"
        :disabled="!canSend"
        @click="submit"
      >
        ↑
      </button>
    </div>
    <span class="composer-hint">AI 可能会出错，重要业务信息请结合引用来源核验。</span>
  </div>
</template>
