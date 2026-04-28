<!--
  QueryToolsPicker — sets per-query overrides (Tools panel).
  UI exposes only the toggles a casual user actually flips:
    • Thinking: Default / Off / On
    • Web search: placeholder ("coming soon")

  ``reasoning_effort`` / ``temperature`` / ``max_tokens`` are still
  accepted by the backend schema (``api/schemas.py:GenerationOverrides``)
  and forwarded to LiteLLM, so power-user paths — direct API calls,
  yaml ``cfg.reasoning_effort`` / ``cfg.temperature``, scripts —
  continue to work. They're just not part of the chat UI to keep
  the popup focused on the two switches users actually toggle.

  Mirrors PathScopePicker's structure: a borderless badge trigger +
  popup that floats up. The trigger label flips to the active i18n
  string when ANY override is set, with a tiny brand dot to make
  the "non-default" state visible at a glance.

  v-model returns ``null`` when everything is at default (so the API
  layer can omit ``generation_overrides`` entirely), or
  ``{reasoning_effort?, temperature?, max_tokens?}`` when at least one
  field is set. Unset fields are omitted (server interprets "use cfg").
-->
<template>
  <div ref="rootEl" class="relative inline-block">
    <!-- Icon-only trigger: ``Tools`` label + chevron would duplicate
         the path picker's anatomy without adding info (the label
         "Tools" doesn't carry data the way "/legal/2024" does for
         the path picker). Tooltip carries discoverability; the
         icon turns brand-colored when ANY override is set, so the
         non-default state is glanceable without a separate dot. -->
    <button
      type="button"
      class="flex items-center justify-center w-7 h-7 rounded-md bg-bg3/70 text-t2 hover:bg-bg3 transition-colors"
      :class="{ 'text-brand': isCustom, '!bg-bg3': open }"
      :title="t('tools.tooltip')"
      :aria-label="t('tools.label')"
      @click="toggle"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="4" y1="21" x2="4" y2="14"/>
        <line x1="4" y1="10" x2="4" y2="3"/>
        <line x1="12" y1="21" x2="12" y2="12"/>
        <line x1="12" y1="8"  x2="12" y2="3"/>
        <line x1="20" y1="21" x2="20" y2="16"/>
        <line x1="20" y1="12" x2="20" y2="3"/>
        <line x1="1"  y1="14" x2="7"  y2="14"/>
        <line x1="9"  y1="8"  x2="15" y2="8"/>
        <line x1="17" y1="16" x2="23" y2="16"/>
      </svg>
    </button>

    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 mb-1.5 w-[260px] rounded-xl border border-line bg-bg shadow-lg py-1.5 z-20"
      >
        <!-- Thinking: explicit on/off/default toggle -->
        <div class="px-3 py-1.5" :title="t('tools.thinking_hint')">
          <div class="flex items-center justify-between mb-1.5">
            <span class="text-[12px] text-t1">{{ t('tools.thinking') }}</span>
          </div>
          <div class="grid grid-cols-3 gap-0.5 p-0.5 rounded-md border border-line">
            <button
              v-for="opt in thinkingOptions"
              :key="String(opt.value)"
              type="button"
              class="px-1 py-0.5 rounded text-[11px] transition-colors"
              :class="thinking === opt.value
                ? 'bg-bg3 text-t1'
                : 'text-t3 hover:text-t2'"
              @click="thinking = opt.value"
            >{{ opt.label }}</button>
          </div>
        </div>

        <!-- Web search: placeholder, disabled -->
        <div class="px-3 py-1.5 opacity-50 cursor-not-allowed">
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-t1">{{ t('tools.web_search') }}</span>
            <span class="text-[10px] text-t3 italic">{{ t('tools.web_search_coming_soon') }}</span>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps({
  modelValue: { type: Object, default: null },   // {reasoning_effort?, temperature?, max_tokens?} | null
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const rootEl = ref(null)

// Local state — only the toggles the UI exposes. Other override
// fields (reasoning_effort, temperature, max_tokens) flow through
// ``modelValue`` if a caller sets them, but the popup doesn't show
// them; they're controllable via API / yaml only.
const thinking = ref(typeof props.modelValue?.thinking === 'boolean' ? props.modelValue.thinking : null)

const thinkingOptions = computed(() => [
  { value: null,  label: t('tools.thinking_default') },
  { value: false, label: t('tools.thinking_off') },
  { value: true,  label: t('tools.thinking_on') },
])

// Trigger highlights when any UI-exposed field is non-default.
// (Other override fields can also be set programmatically — they
// don't light the icon since the user has no UI for them anyway.)
const isCustom = computed(() => thinking.value != null)

// Emit: preserve any non-UI fields the parent set (so API callers
// can ship reasoning_effort + temperature alongside thinking) while
// updating the bits we own.
watch(thinking, () => {
  const passthrough = { ...(props.modelValue || {}) }
  delete passthrough.thinking
  const out = { ...passthrough }
  if (thinking.value != null) out.thinking = thinking.value
  emit('update:modelValue', Object.keys(out).length ? out : null)
})

// External resets sync back.
watch(() => props.modelValue, (v) => {
  thinking.value = typeof v?.thinking === 'boolean' ? v.thinking : null
})

function toggle() { open.value = !open.value }
function close() { open.value = false }

// Click-outside
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
