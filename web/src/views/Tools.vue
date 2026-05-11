<template>
  <!--
    Tools station (工具站) — the unified home for agent CAPABILITY
    management. Two halves under one roof:

      Tools — single-call agent capabilities (one MCP server publishes
              N tools: search_vector / read_chunk / web_fetch / bash /
              edit / etc.). Listed here so the user can see what the
              agent can DO and toggle individual plugin sources on or
              off. The MCP registration lives in api/routes/mcp_server.py;
              this page becomes the operator's view of that registry.

      Skills — multi-step agent presets the team shares ("weekly
               report builder", "contract comparator"). Each skill
               wraps a prompt + tool config + suggested context so a
               non-power user clicks it and goes. Extension of the
               existing prompt-presets the chat composer surfaces in
               the empty state.

    For now both halves render placeholder content — the real
    implementation lands across multiple commits (dynamic MCP
    registry first, then skill schema + presets editor).
  -->
  <div class="tools-page">
    <header class="page-header">
      <div>
        <h1 class="page-title">{{ t('tools_page.title') }}</h1>
        <p class="page-hint">{{ t('tools_page.subtitle') }}</p>
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
        <span v-if="tab.count != null" class="tab__count">{{ tab.count }}</span>
      </button>
    </div>

    <div v-if="active === 'tools'" class="tab-body">
      <div v-if="toolList.length === 0" class="placeholder">
        <div class="placeholder-icon"><Wrench :size="28" :stroke-width="1.25" /></div>
        <h2 class="placeholder-title">{{ t('tools_page.tools_tab.coming_soon') }}</h2>
        <p class="placeholder-text">{{ t('tools_page.tools_tab.placeholder_desc') }}</p>
      </div>
      <div v-else class="tool-grid">
        <div v-for="grp in toolList" :key="grp.plugin" class="tool-group">
          <div class="tool-group__head">{{ grp.plugin }}</div>
          <ul class="tool-group__items">
            <li v-for="tool in grp.tools" :key="tool.name" class="tool-row">
              <code class="tool-row__name">{{ tool.name }}</code>
              <span class="tool-row__desc">{{ tool.description }}</span>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <div v-else-if="active === 'skills'" class="tab-body">
      <div class="placeholder">
        <div class="placeholder-icon"><Sparkles :size="28" :stroke-width="1.25" /></div>
        <h2 class="placeholder-title">{{ t('tools_page.skills_tab.coming_soon') }}</h2>
        <p class="placeholder-text">{{ t('tools_page.skills_tab.placeholder_desc') }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Wrench, Sparkles } from 'lucide-vue-next'

const { t } = useI18n()

const active = ref('tools')

// Placeholder: when the dynamic plugin registry lands, this becomes
// a real fetch against ``/api/v1/agent/tools`` (or similar) grouped
// by their MCP server name. For now we render an empty placeholder
// so the IA is visible.
const toolList = computed(() => [])

const TABS = computed(() => [
  { id: 'tools',  label: 'tools_page.tabs.tools',  icon: Wrench,   count: null },
  { id: 'skills', label: 'tools_page.tabs.skills', icon: Sparkles, count: null },
])
</script>

<style scoped>
.tools-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
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
.tab__count {
  margin-left: 4px;
  padding: 0 6px;
  background: var(--color-bg3);
  border-radius: 10px;
  font-size: 0.625rem;
  color: var(--color-t2);
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

.tool-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
}
.tool-group {
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 10px;
  overflow: hidden;
}
.tool-group__head {
  padding: 10px 14px;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-t1);
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg);
}
.tool-group__items { list-style: none; margin: 0; padding: 6px 0; }
.tool-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 6px 14px;
}
.tool-row__name {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  color: var(--color-brand);
  flex-shrink: 0;
}
.tool-row__desc {
  font-size: 0.6875rem;
  color: var(--color-t2);
  line-height: 1.5;
}
</style>
