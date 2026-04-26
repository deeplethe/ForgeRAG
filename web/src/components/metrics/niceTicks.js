/**
 * Compute "nice" axis ticks for a numeric range.
 *
 * Standard chart algorithm: pick a step from {1, 2, 5} × 10^N that yields
 * roughly `target` ticks across the [0, max] range. Returns the rounded-up
 * domain max + the step + the explicit tick array.
 *
 * Options:
 *   integer: true  →  step is at least 1 and snapped to integer (use for
 *                     count-style charts like "tokens" where 0.5 makes no
 *                     sense; otherwise small ranges produce 0.333/0.666 etc.)
 *
 * Example:
 *   niceTicks(0, 1, 4, { integer: true })  →
 *     { max: 1, step: 1, ticks: [0, 1] }   // 2 ticks instead of 4 fractions
 *   niceTicks(0, 4200, 4)  →
 *     { max: 5000, step: 1000, ticks: [0, 1000, 2000, 3000, 4000, 5000] }
 */
export function niceTicks(min, max, target = 4, opts = {}) {
  const integer = !!opts.integer
  if (max <= min) {
    const m = integer ? 1 : 1
    return { max: m, step: m, ticks: [0, m] }
  }
  const range = max - min
  let step = niceNum(range / Math.max(1, target - 1), true)
  if (integer) step = Math.max(1, Math.round(step))
  const niceMin = Math.floor(min / step) * step
  const niceMax = Math.ceil(max / step) * step
  const ticks = []
  for (let v = niceMin; v <= niceMax + step / 2; v += step) {
    // round-trip through Number to avoid 0.1 + 0.2 = 0.3000004 garbage
    ticks.push(Math.round(v / step) * step)
  }
  return { max: niceMax, step, ticks }
}

/** Round a value to a "nice" number (1, 2, 5, 10 × 10^N). */
function niceNum(value, round) {
  if (value === 0) return 0
  const exp = Math.floor(Math.log10(value))
  const f = value / Math.pow(10, exp)
  let nf
  if (round) {
    if (f < 1.5) nf = 1
    else if (f < 3) nf = 2
    else if (f < 7) nf = 5
    else nf = 10
  } else {
    if (f <= 1) nf = 1
    else if (f <= 2) nf = 2
    else if (f <= 5) nf = 5
    else nf = 10
  }
  return nf * Math.pow(10, exp)
}
