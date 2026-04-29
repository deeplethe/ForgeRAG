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
      <!-- 4-pointed sparkles — same icon used in Thinking panes
           (Chat.vue history + live streaming pane). Keeps the
           "AI/reasoning" visual language consistent across the app. -->
      <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
        <path fill-rule="evenodd" clip-rule="evenodd" d="M9.97165 1.29981C11.5853 0.718916 13.271 0.642197 14.3144 1.68555C15.3577 2.72902 15.2811 4.41466 14.7002 6.02833C14.4707 6.66561 14.1504 7.32937 13.75 8.00001C14.1504 8.67062 14.4707 9.33444 14.7002 9.97169C15.2811 11.5854 15.3578 13.271 14.3144 14.3145C13.271 15.3579 11.5854 15.2811 9.97165 14.7002C9.3344 14.4708 8.67059 14.1505 7.99997 13.75C7.32933 14.1505 6.66558 14.4708 6.02829 14.7002C4.41461 15.2811 2.72899 15.3578 1.68552 14.3145C0.642155 13.271 0.71887 11.5854 1.29977 9.97169C1.52915 9.33454 1.84865 8.67049 2.24899 8.00001C1.84866 7.32953 1.52915 6.66544 1.29977 6.02833C0.718852 4.41459 0.64207 2.729 1.68552 1.68555C2.72897 0.642112 4.41456 0.718887 6.02829 1.29981C6.66541 1.52918 7.32949 1.8487 7.99997 2.24903C8.67045 1.84869 9.33451 1.52919 9.97165 1.29981ZM12.9404 9.2129C12.4391 9.893 11.8616 10.5681 11.2148 11.2149C10.568 11.8616 9.89296 12.4391 9.21286 12.9404C9.62532 13.1579 10.0271 13.338 10.4121 13.4766C11.9146 14.0174 12.9172 13.8738 13.3955 13.3955C13.8737 12.9173 14.0174 11.9146 13.4765 10.4121C13.3379 10.0271 13.1578 9.62535 12.9404 9.2129ZM3.05856 9.2129C2.84121 9.62523 2.66197 10.0272 2.52341 10.4121C1.98252 11.9146 2.12627 12.9172 2.60446 13.3955C3.08278 13.8737 4.08544 14.0174 5.58786 13.4766C5.97264 13.338 6.37389 13.1577 6.7861 12.9404C6.10624 12.4393 5.43168 11.8614 4.78513 11.2149C4.13823 10.5679 3.55992 9.89313 3.05856 9.2129ZM7.99899 3.792C7.23179 4.31419 6.45306 4.95512 5.70407 5.70411C4.95509 6.45309 4.31415 7.23184 3.79196 7.99903C4.3143 8.76666 4.95471 9.54653 5.70407 10.2959C6.45309 11.0449 7.23271 11.6848 7.99997 12.207C8.76725 11.6848 9.54683 11.0449 10.2959 10.2959C11.0449 9.54686 11.6848 8.76729 12.207 8.00001C11.6848 7.23275 11.0449 6.45312 10.2959 5.70411C9.5465 4.95475 8.76662 4.31434 7.99899 3.792ZM5.58786 2.52344C4.08533 1.98255 3.08272 2.12625 2.60446 2.6045C2.12621 3.08275 1.98252 4.08536 2.52341 5.5879C2.66189 5.97253 2.8414 6.37409 3.05856 6.78614C3.55983 6.10611 4.1384 5.43189 4.78513 4.78516C5.43186 4.13843 6.10606 3.55987 6.7861 3.0586C6.37405 2.84144 5.97249 2.66192 5.58786 2.52344ZM13.3955 2.6045C12.9172 2.12631 11.9146 1.98257 10.4121 2.52344C10.0272 2.66201 9.62519 2.84125 9.21286 3.0586C9.8931 3.55996 10.5679 4.13827 11.2148 4.78516C11.8614 5.43172 12.4392 6.10627 13.9404 6.78614C13.1577 6.37393 13.338 5.97267 13.4765 5.5879C14.0174 4.08549 13.8736 3.08281 13.3955 2.6045Z"/>
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
        class="absolute bottom-full left-0 mb-1.5 w-[180px] rounded-xl border border-line bg-bg shadow-lg p-1 z-20 space-y-0.5"
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

// Default → On → Off — Default leads because it's the recommended
// state (most users never override). The two explicit overrides
// follow in "more thinking" → "less thinking" order: On surfaces
// before Off so the picker reads as a positive-action menu rather
// than a "did you mean to disable this?" cue.
const options = computed(() => [
  { value: null,  label: t('tools.thinking_default') },
  { value: true,  label: t('tools.thinking_on') },
  { value: false, label: t('tools.thinking_off') },
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
