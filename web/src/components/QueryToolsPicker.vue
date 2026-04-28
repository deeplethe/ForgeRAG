<!--
  QueryToolsPicker — sets per-query overrides (Tools panel).
  Phase 1 controls:
    • Reasoning effort: Default / Low / Medium / High / Off
    • Temperature: Default + slider (0 - 2)
    • Web search: placeholder ("coming soon")

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
    <button
      type="button"
      class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-bg3/70 text-[11px] text-t2 hover:bg-bg3 transition-colors"
      :class="{ 'text-brand': isCustom, '!bg-bg3': open }"
      :title="t('tools.tooltip')"
      @click="toggle"
    >
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round">
        <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/>
      </svg>
      <span>{{ t('tools.label') }}</span>
      <span v-if="isCustom" class="w-1.5 h-1.5 rounded-full bg-brand" />
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="ml-0.5 transition-transform" :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>

    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 mb-1.5 w-[300px] rounded-xl border border-line bg-bg shadow-lg py-1.5 z-20"
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

        <!-- Reasoning effort (intensity dial — orthogonal to thinking on/off) -->
        <div class="px-3 py-1.5" :title="t('tools.effort_hint')">
          <div class="flex items-center justify-between mb-1.5">
            <span class="text-[12px] text-t1">{{ t('tools.reasoning_effort') }}</span>
          </div>
          <div class="grid grid-cols-4 gap-0.5 p-0.5 rounded-md border border-line">
            <button
              v-for="opt in effortOptions"
              :key="opt.value ?? 'default'"
              type="button"
              class="px-1 py-0.5 rounded text-[11px] transition-colors"
              :class="effort === opt.value
                ? 'bg-bg3 text-t1'
                : 'text-t3 hover:text-t2'"
              @click="effort = opt.value"
            >{{ opt.label }}</button>
          </div>
        </div>

        <!-- Temperature: slider -->
        <div class="px-3 py-1.5">
          <div class="flex items-center justify-between mb-1.5">
            <span class="text-[12px] text-t1">{{ t('tools.temperature') }}</span>
            <span class="text-[11px] text-t3 tabular-nums">
              {{ temperature == null ? t('tools.temperature_default') : temperature.toFixed(1) }}
            </span>
          </div>
          <input
            type="range"
            min="0" max="2" step="0.1"
            :value="temperature ?? 0.1"
            @input="onTemperatureInput"
            class="w-full h-1 bg-bg3 rounded-full appearance-none cursor-pointer
                   [&::-webkit-slider-thumb]:appearance-none
                   [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3
                   [&::-webkit-slider-thumb]:rounded-full
                   [&::-webkit-slider-thumb]:bg-t1
                   [&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-line"
          />
          <div class="text-[10px] text-t3 mt-0.5">{{ t('tools.temperature_hint') }}</div>
          <button
            v-if="temperature != null"
            class="mt-1 text-[10px] text-t3 hover:text-t1"
            @click="temperature = null"
          >{{ t('tools.temperature_default') }}</button>
        </div>

        <!-- Web search: placeholder, disabled -->
        <div class="px-3 py-1.5 opacity-50 cursor-not-allowed">
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-t1">{{ t('tools.web_search') }}</span>
            <span class="text-[10px] text-t3 italic">{{ t('tools.web_search_coming_soon') }}</span>
          </div>
        </div>

        <!-- Reset all -->
        <div v-if="isCustom" class="px-3 py-1.5 border-t border-line">
          <button
            type="button"
            class="w-full text-[11px] text-t2 hover:text-t1 text-center py-1 rounded hover:bg-bg3 transition-colors"
            @click="resetAll"
          >{{ t('tools.reset') }}</button>
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

// Local state — synced TO/FROM modelValue.
// ``thinking`` is tri-state: null (default) / true (on) / false (off).
const thinking = ref(typeof props.modelValue?.thinking === 'boolean' ? props.modelValue.thinking : null)
const effort = ref(props.modelValue?.reasoning_effort ?? null)
const temperature = ref(
  typeof props.modelValue?.temperature === 'number' ? props.modelValue.temperature : null,
)

const thinkingOptions = computed(() => [
  { value: null,  label: t('tools.thinking_default') },
  { value: false, label: t('tools.thinking_off') },
  { value: true,  label: t('tools.thinking_on') },
])
const effortOptions = computed(() => [
  { value: null,     label: t('tools.effort_default') },
  { value: 'low',    label: t('tools.effort_low') },
  { value: 'medium', label: t('tools.effort_medium') },
  { value: 'high',   label: t('tools.effort_high') },
])

// Trigger highlights when any field is non-default
const isCustom = computed(() =>
  thinking.value != null || effort.value != null || temperature.value != null,
)

// Emit aggregated overrides ({} → null so API layer can omit the field).
watch([thinking, effort, temperature], () => {
  const out = {}
  if (thinking.value != null) out.thinking = thinking.value
  if (effort.value != null) out.reasoning_effort = effort.value
  if (temperature.value != null) out.temperature = temperature.value
  emit('update:modelValue', Object.keys(out).length ? out : null)
}, { deep: false })

// External resets (e.g. parent clearing) sync back to local.
watch(() => props.modelValue, (v) => {
  thinking.value = typeof v?.thinking === 'boolean' ? v.thinking : null
  effort.value = v?.reasoning_effort ?? null
  temperature.value = typeof v?.temperature === 'number' ? v.temperature : null
})

function toggle() { open.value = !open.value }
function close() { open.value = false }

function onTemperatureInput(e) {
  // Slider always emits a number, but we want to allow "null" (default)
  // by tapping the reset button. Here we just record the value.
  temperature.value = parseFloat(e.target.value)
}

function resetAll() {
  thinking.value = null
  effort.value = null
  temperature.value = null
}

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
