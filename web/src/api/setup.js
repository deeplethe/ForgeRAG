/**
 * First-boot setup wizard API client.
 *
 * All endpoints under /api/v1/setup/ are unauthenticated by design
 * (the operator hasn't created an account yet) and self-disable
 * once the deploy is configured — see api/routes/setup.py.
 */

import { get, post } from './client'

/** Probe whether the deploy needs the wizard. Cheap; safe to poll.
 *  Returns ``{configured, blockers, suggested_locale}``. */
export const getSetupStatus = () =>
  get('/api/v1/setup/status')

/** Static catalog of preset tiles (SiliconFlow / OpenAI / ...). */
export const listSetupPresets = () =>
  get('/api/v1/setup/presets')

/** Round-trip a 1-token chat completion to validate the user's
 *  preset + key. Returns ``{ok, error?, latency_ms?, model?}``. */
export const testSetupLlm = (preset_id, inputs) =>
  post('/api/v1/setup/test-llm', { preset_id, inputs })

/** Persist the chosen preset to the overlay yaml + signal the
 *  worker to restart. Returns ``{ok, overlay_path, restart_scheduled}``. */
export const commitSetup = (preset_id, inputs) =>
  post('/api/v1/setup/commit', { preset_id, inputs })
