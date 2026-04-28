<!--
  ThinkingPicker — small inline chip for the per-query thinking
  toggle. Chip + popup pattern (mirrors PathScopePicker), but the
  popup only carries 3 options (Default / Off / On) so it's a
  one-row segmented control rather than a settings panel.

  The chip's label reflects the current state so the user can see
  "On" / "Off" / "Default" without opening the popup. When set to
  anything other than ``Default`` (the implicit state), the chip's
  icon turns brand-colored.

  v-model: ``bool | null`` directly. Chat.vue maps this to the
  ``thinking`` field of the ``generation_overrides`` POST payload.
-->
<template>
  <div ref="rootEl" class="relative inline-block">
    <button
      type="button"
      class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-bg3/70 text-[11px] text-t2 hover:bg-bg3 transition-colors"
      :class="{ 'text-brand': modelValue !== null, '!bg-bg3': open }"
      :title="t('tools.thinking_hint')"
      @click="toggle"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
        <path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/>
        <line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      <span>{{ t('tools.thinking') }}</span>
      <span class="text-t3">·</span>
      <span>{{ stateLabel }}</span>
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="ml-0.5 transition-transform" :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>

    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 mb-1.5 w-[180px] rounded-xl border border-line bg-bg shadow-lg p-1 z-20"
      >
        <button
          v-for="opt in options"
          :key="String(opt.value)"
          type="button"
          class="w-full flex items-center justify-between px-2.5 py-1.5 rounded-md text-[12px] transition-colors"
          :class="modelValue === opt.value
            ? 'bg-bg3 text-t1'
            : 'text-t2 hover:bg-bg3'"
          @click="pick(opt.value)"
        >
          <span>{{ opt.label }}</span>
          <svg v-if="modelValue === opt.value" width="11" height="11" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" stroke-width="3" class="text-brand">
            <path d="M20 6L9 17l-5-5"/>
          </svg>
        </button>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const props = defineProps({ modelValue: { type: [Boolean, Object], default: null } })
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const rootEl = ref(null)

const options = computed(() => [
  { value: null,  label: t('tools.thinking_default') },
  { value: false, label: t('tools.thinking_off') },
  { value: true,  label: t('tools.thinking_on') },
])

const stateLabel = computed(() => {
  if (props.modelValue === null) return t('tools.thinking_default')
  return props.modelValue ? t('tools.thinking_on') : t('tools.thinking_off')
})

function toggle() { open.value = !open.value }
function close() { open.value = false }
function pick(v) { emit('update:modelValue', v); close() }

function onDocClick(e) {
  if (!open.value || !rootEl.value) return
  if (!rootEl.value.contains(e.target)) close()
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<style scoped>
.popup-enter-active, .popup-leave-active { transition: opacity .15s ease, transform .15s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(4px); }
</style>
