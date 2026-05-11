<template>
  <!--
    Tasks — daily-use surface for long-running and scheduled agent
    work. Sits at the main nav rather than Settings because it's
    operational ("what's the agent doing for me right now?") not
    configuration.

    Two halves under one roof:

      Active — in-flight long tasks. Shows running agent_runs the
               user has kicked off (across all conversations);
               click a row to jump to the chat that owns it.
               Lands together with the backend "agent survives
               client disconnect" refactor — when that ships, this
               page becomes the "I closed the page, what's still
               cooking?" view.

      Scheduled — recurring / cron-style agent runs ("every Monday
                  morning, summarize last week's uploads"). Real
                  scheduler lives behind /api/v1/agent/scheduled
                  once the cron registry lands.

    Placeholder for now; the IA + sidebar entry ship first so the
    user discovers the surface before the underlying machinery is
    finished.
  -->
  <div class="tasks-page">
    <header class="page-header">
      <div>
        <h1 class="page-title">{{ t('tasks.title') }}</h1>
        <p class="page-hint">{{ t('tasks.subtitle') }}</p>
      </div>
    </header>

    <div class="tabs">
      <button
        v-for="tab in TABS"
        :key="tab.id"
        class="tab"
        :class="{ 'tab--active': active === tab.id }"
        @click="active = tab.id"
      >
        <component :is="tab.icon" :size="14" :stroke-width="1.6" />
        <span>{{ t(tab.label) }}</span>
      </button>
    </div>

    <div v-if="active === 'active'" class="tab-body">
      <div class="placeholder">
        <div class="placeholder-icon"><Activity :size="28" :stroke-width="1.25" /></div>
        <h2 class="placeholder-title">{{ t('tasks.active_tab.coming_soon') }}</h2>
        <p class="placeholder-text">{{ t('tasks.active_tab.placeholder_desc') }}</p>
      </div>
    </div>

    <div v-else-if="active === 'scheduled'" class="tab-body">
      <div class="placeholder">
        <div class="placeholder-icon"><CalendarClock :size="28" :stroke-width="1.25" /></div>
        <h2 class="placeholder-title">{{ t('tasks.scheduled_tab.coming_soon') }}</h2>
        <p class="placeholder-text">{{ t('tasks.scheduled_tab.placeholder_desc') }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Activity, CalendarClock } from 'lucide-vue-next'

const { t } = useI18n()
const active = ref('active')

const TABS = computed(() => [
  { id: 'active',    label: 'tasks.tabs.active',    icon: Activity },
  { id: 'scheduled', label: 'tasks.tabs.scheduled', icon: CalendarClock },
])
</script>

<style scoped>
.tasks-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}
.page-header {
  padding: 18px 28px 0;
}
.page-title { font-size: 1.125rem; font-weight: 600; color: var(--color-t1); margin: 0; }
.page-hint { margin: 6px 0 0; font-size: 0.8125rem; color: var(--color-t2); line-height: 1.55; }

.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 14px 28px 0;
  border-bottom: 1px solid var(--color-line);
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 10px;
  margin-bottom: -1px;
  border: none;
  background: transparent;
  color: var(--color-t3);
  font-size: 0.75rem;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}
.tab:hover { color: var(--color-t1); }
.tab--active {
  color: var(--color-t1);
  border-bottom-color: var(--color-brand);
}

.tab-body {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}

.placeholder {
  margin-top: 24px;
  padding: 56px 24px;
  background: var(--color-bg2);
  border: 1px dashed var(--color-line);
  border-radius: 10px;
  text-align: center;
  color: var(--color-t2);
}
.placeholder-icon { color: var(--color-t3); margin-bottom: 12px; }
.placeholder-title { font-size: 0.875rem; font-weight: 500; color: var(--color-t1); margin: 0 0 6px; }
.placeholder-text { font-size: 0.75rem; color: var(--color-t3); margin: 0; line-height: 1.6; max-width: 480px; margin-inline: auto; }
</style>
