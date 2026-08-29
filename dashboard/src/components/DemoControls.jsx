import { useState } from 'react'
import { api } from '../api.js'

/**
 * Demo controls.
 *
 * The outage pair is the important one. `Break HDFC cards` trips a circuit in
 * about a second and the issuer panel goes red; `Recover HDFC` walks it back
 * through half-open to closed and releases the held retries. That sequence is the
 * clearest twenty seconds of the demo, because it shows the system declining to
 * do the obvious wrong thing while the outage is live.
 *
 * The response text is shown verbatim rather than reduced to a toast. Recovery
 * reports the path the state machine actually took — `open → half_open → closed`
 * — and that path is the evidence that the wait was compressed rather than the
 * state machine bypassed.
 */
export default function DemoControls({ act, busy, live, setLive, refresh }) {
  const [note, setNote] = useState('')

  const run = async (label, fn) => {
    setNote(`${label}…`)
    const res = await act(fn)
    if (!res) return setNote('')
    setNote(res.note ?? summarize(label, res))
    return undefined
  }

  return (
    <div>
      <div className="controls">
        <button
          className="primary"
          disabled={busy}
          onClick={() => run('Seeding 400 failed payments', () => api.seed({ count: 400, span_minutes: 180 }))}
        >
          Seed 400 failures
        </button>
        <button disabled={busy} onClick={() => run('Advancing the queue', () => api.tick(200))}>
          Advance queue
        </button>
        <button
          className="warn"
          disabled={busy}
          onClick={() => run('Breaking HDFC cards', () => api.outage('HDFC', 'card'))}
        >
          Break HDFC cards
        </button>
        <button
          className="good"
          disabled={busy}
          onClick={() => run('Recovering HDFC', () => api.recoverIssuer('HDFC', 'card'))}
        >
          Recover HDFC
        </button>
        <button className="danger" disabled={busy} onClick={() => run('Resetting', () => api.reset())}>
          Reset
        </button>

        <label className="toggle" onClick={() => setLive(!live)}>
          <span className={`dot ${live ? 'on' : ''}`} />
          {live ? 'live' : 'paused'}
        </label>
        <button disabled={busy} onClick={refresh} title="Refresh now">
          ↻
        </button>
      </div>

      {note && (
        <p
          className="note-line"
          style={{ textAlign: 'right', maxWidth: '62ch', marginLeft: 'auto' }}
        >
          {note}
        </p>
      )}
    </div>
  )
}

/** Fallback one-liner for the endpoints that do not return a `note` of their own. */
function summarize(label, res) {
  if (typeof res.seeded === 'number')
    return (
      `${label}: ${res.seeded} failed payments ingested through the signed webhook path, ` +
      `${res.baseline_successes} baseline authorizations played in for the circuit breaker, ` +
      `${res.attempts_executed_immediately ?? 0} attempts fired immediately.`
    )
  if (typeof res.handled === 'number') return `${label}: ${res.handled} attempt(s) processed.`
  if (res.stats) return `${label}: done.`
  return `${label}: ok.`
}
