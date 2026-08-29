import { useState } from 'react'
import {
  clock,
  humanize,
  num,
  OUTCOME_TONE,
  relative,
  rupees,
  traceKind,
} from '../fmt.js'

/**
 * Live decision log, one row per recovery attempt, expandable into the full
 * decision trace.
 *
 * This is the panel that decides whether a reviewer believes the rest of the
 * screen. Every other number here is an aggregate, and an aggregate cannot be
 * argued with — you either trust it or you don't. A trace can: it names the
 * bucket, the confidence, the rule that fired, each guardrail that passed, the one
 * that vetoed, the rail it steered to and why, the delay it chose and what that
 * delay was waiting for. Click any row and the system explains itself in its own
 * words.
 *
 * The trace is generated during the decision, not reconstructed afterwards from
 * the outcome — which is why it can show a veto on an attempt that never happened.
 * A log written after the fact can only ever explain what did occur.
 */
export default function ActivityFeed({ attempts }) {
  const [open, setOpen] = useState(() => new Set())
  const rows = attempts ?? []

  const toggle = (id) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className="panel span2">
      <h2>Decision log</h2>
      <p className="sub">
        Newest first, with a reserved slice for suppressions so the decisions{' '}
        <i>not</i> to act stay on screen. Click a row for the trace the agent wrote while
        deciding — every gate it checked, the rail it chose, and the reason for the delay.
      </p>

      {!rows.length ? (
        <div className="empty">No attempts yet.</div>
      ) : (
        <div className="feed">
          {rows.map((a) => {
            const isOpen = open.has(a.id)
            const tone = OUTCOME_TONE[a.outcome] ?? 'muted'
            const scheduled = !a.executed_at && a.scheduled_for
            return (
              <div className="row" key={a.id}>
                <button
                  className="row-head"
                  onClick={() => toggle(a.id)}
                  aria-expanded={isOpen}
                >
                  <span className="caret">{isOpen ? '▾' : '▸'}</span>
                  <span className={`pill ${tone}`}>
                    {a.outcome === 'suppressed' ? 'held' : humanize(a.outcome)}
                  </span>
                  <span className="who">
                    <b>{humanize(a.bucket)}</b> <em>· {humanize(a.action)}</em>{' '}
                    <em>
                      · {a.rail} · attempt {a.attempt_no}
                      {a.deferrals ? ` · deferred ${num(a.deferrals)}×` : ''}
                    </em>
                  </span>
                  <span className="pill muted">
                    {scheduled ? relative(a.scheduled_for) : clock(a.executed_at)}
                  </span>
                  <span
                    className="amt"
                    style={{ color: a.recovered_paise ? 'var(--good)' : 'var(--dim)' }}
                  >
                    {rupees(a.recovered_paise || a.amount_paise)}
                  </span>
                </button>

                {isOpen && (
                  <>
                    <div className="trace">
                      {(a.decision_trace ?? []).map((line, i) => (
                        <span key={i} className={traceKind(line)}>
                          {line}
                        </span>
                      ))}
                      {!a.decision_trace?.length && (
                        <span className="note">No trace recorded.</span>
                      )}
                    </div>

                    {a.message_body && (
                      <div className="msg">
                        <span className="lbl">
                          message sent to customer
                          {a.contacted_customer ? '' : ' (not sent — no channel)'}
                        </span>
                        {a.message_body}
                      </div>
                    )}

                    <div className="msg" style={{ background: 'transparent', border: 'none', paddingLeft: 0 }}>
                      <span className="lbl">record</span>
                      <span style={{ fontFamily: 'var(--mono)', fontSize: '11.5px', color: 'var(--dim)' }}>
                        order {a.order_id} · payment {a.payment_id} · issuer{' '}
                        {a.issuer_key ?? '—'} · idempotency <b>{a.idempotency_key}</b>
                        {a.suppression_reason ? ` · suppressed: ${humanize(a.suppression_reason)}` : ''}
                        {a.payment_link_url ? ' · ' : ''}
                      </span>
                      {a.payment_link_url && (
                        <a href={a.payment_link_url} target="_blank" rel="noreferrer">
                          open recovery link ↗
                        </a>
                      )}
                    </div>
                  </>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
