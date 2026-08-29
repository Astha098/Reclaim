/**
 * Formatting helpers.
 *
 * Money is shown in lakh and crore rather than millions. This is not decoration:
 * the audience for this dashboard is Indian payments operators, and "₹3.05Cr"
 * is the unit they actually reason in. `Intl.NumberFormat('en-IN')` also gives
 * the 2-2-3 digit grouping (₹1,45,294 not ₹145,294) that makes a rupee figure
 * readable at a glance to that audience.
 */

const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })

/** Full rupee amount from paise: 14529400 → "₹1,45,294" */
export function rupees(paise) {
  return '₹' + inr.format(Math.round((paise ?? 0) / 100))
}

/** Compact rupee amount from paise: 3050000000 → "₹3.05Cr" */
export function rupeesShort(paise) {
  const r = (paise ?? 0) / 100
  if (r >= 1e7) return `₹${(r / 1e7).toFixed(2)}Cr`
  if (r >= 1e5) return `₹${(r / 1e5).toFixed(2)}L`
  if (r >= 1e3) return `₹${(r / 1e3).toFixed(1)}K`
  return '₹' + inr.format(Math.round(r))
}

export function pct(x, digits = 1) {
  return `${((x ?? 0) * 100).toFixed(digits)}%`
}

export function num(n) {
  return inr.format(n ?? 0)
}

/** "insufficient_funds" → "insufficient funds" — labels, not identifiers. */
export function humanize(s) {
  return (s ?? '').replaceAll('_', ' ')
}

/** Wall-clock time only. The dashboard is a live view; the date is the header's job. */
export function clock(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** "2026-08-22T09" (the timeline's hour key) → "09:00" */
export function hourLabel(key) {
  const hh = (key ?? '').slice(11, 13)
  return hh ? `${hh}:00` : '—'
}

/** A future-dated retry, phrased as a delay: "in 4.0d", "in 35m", "now". */
export function relative(iso, from = Date.now()) {
  if (!iso) return '—'
  const ms = new Date(iso).getTime() - from
  if (Number.isNaN(ms)) return '—'
  const abs = Math.abs(ms)
  const unit =
    abs < 60_000
      ? `${Math.round(abs / 1000)}s`
      : abs < 3_600_000
        ? `${Math.round(abs / 60_000)}m`
        : abs < 86_400_000
          ? `${(abs / 3_600_000).toFixed(1)}h`
          : `${(abs / 86_400_000).toFixed(1)}d`
  if (abs < 30_000) return 'now'
  return ms > 0 ? `in ${unit}` : `${unit} ago`
}

/**
 * Classify a decision-trace line by its leading glyph, so the feed can colour it.
 * The backend writes ✓ for a passed gate, ✗ for a veto, → for an effect.
 */
export function traceKind(line) {
  const c = (line ?? '').trimStart()[0]
  if (c === '✓') return 'pass'
  if (c === '✗') return 'veto'
  if (c === '→') return 'effect'
  return 'note'
}

export const OUTCOME_TONE = {
  recovered: 'good',
  suppressed: 'muted',
  no_response: 'warn',
  failed: 'bad',
  deferred: 'info',
  pending: 'info',
  scheduled: 'info',
  error: 'bad',
}
