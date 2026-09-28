<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
defineProps<{ title: string; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const root = ref<HTMLElement>()
let previous: HTMLElement | null = null
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  if (event.key === 'Tab') {
    const nodes = root.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, textarea, [tabindex="0"]')
    if (!nodes?.length) return
    const first = nodes[0], last = nodes[nodes.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
}
onMounted(() => { previous = document.activeElement as HTMLElement; root.value?.focus(); document.addEventListener('keydown', key) })
onUnmounted(() => { document.removeEventListener('keydown', key); previous?.focus() })
</script>
<template>
  <div class="modal-backdrop" @mousedown.self="emit('close')">
    <section ref="root" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1" class="modal" :class="{ wide }">
      <header class="modal-header"><h2>{{ title }}</h2><button class="icon-button" aria-label="关闭窗口" @click="emit('close')">×</button></header>
      <div class="modal-body"><slot /></div>
    </section>
  </div>
</template>
