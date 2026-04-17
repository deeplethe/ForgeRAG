import { get, post } from './client'

/**
 * Start a benchmark run.
 * @param {Object} opts
 * @param {number} [opts.numQuestions=30]       Generate this many new questions (ignored if replay).
 * @param {string} [opts.replayFromRunId]       Reuse questions from a prior run saved on disk.
 * @param {Array}  [opts.replayItems]           Or pass questions inline (each with question+ground_truth).
 */
export function startBenchmark({ numQuestions = 30, replayFromRunId, replayItems } = {}) {
  const body = { num_questions: numQuestions }
  if (replayFromRunId) body.replay_from_run_id = replayFromRunId
  if (replayItems) body.replay_items = replayItems
  return post('/api/v1/benchmark/start', body)
}

export function cancelBenchmark() {
  return post('/api/v1/benchmark/cancel')
}

export function getBenchmarkStatus() {
  return get('/api/v1/benchmark/status')
}

export function listBenchmarkReports() {
  return get('/api/v1/benchmark/reports')
}

export function downloadBenchmarkReport() {
  // Direct download — returns a blob
  const base = import.meta.env.VITE_API_BASE || ''
  return `${base}/api/v1/benchmark/report`
}
