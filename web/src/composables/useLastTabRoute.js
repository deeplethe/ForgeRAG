/**
 * Sidebar-tab "remember last fullPath" registry.
 *
 * Why this exists: ``<KeepAlive>`` already preserves component state
 * across tab switches, but the sidebar's tab links point to bare
 * ``/library`` / ``/workspace`` without any ``?path=`` query. Clicking
 * the sidebar tab rewrites the route to the bare form, and the cached
 * component's watcher on ``route.query.path`` then resets it back to
 * the root — which feels broken to the user (they came back to a
 * different folder than they left).
 *
 * Module-level reactive map so it's shared across the app without
 * pulling in a Pinia store for one piece of state. Views call
 * ``set(tabPath, fullPath)`` whenever their internal location
 * changes; the sidebar calls ``get(tabPath)`` to navigate to the
 * remembered location instead of the bare tab path.
 */
import { reactive } from 'vue'

const _byTab = reactive({})

export function useLastTabRoute() {
  return {
    /**
     * Record the most recent fullPath the user was on for a given
     * tab. ``tabPath`` is the bare sidebar link (``/library``,
     * ``/workspace``); ``fullPath`` is what router would call
     * ``route.fullPath`` (path + query).
     */
    set(tabPath, fullPath) {
      if (!tabPath || !fullPath) return
      _byTab[tabPath] = fullPath
    },

    /**
     * Resolve a sidebar-click target. If we've recorded a previous
     * fullPath for this tab, return it; otherwise fall back to the
     * bare tab path (first-time visit).
     */
    get(tabPath) {
      return _byTab[tabPath] || tabPath
    },
  }
}
