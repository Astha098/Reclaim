import { num, pct, rupees, rupeesShort } from '../fmt.js'

/**
 * The four numbers that matter, with recovered revenue given the largest type on
 * the page.
 *
 * The lost-revenue card is deliberately next to it rather than hidden: recovered
 * rupees mean nothing without the denominator, and a dashboard that shows only
 * its wins is the kind of dashboard that gets a system shut down after the first
 * finance review.
 */
export default function Headline({ stats, sched }) {
  if (!stats) return null

  const outstanding = (stats.failed_value_paise ?? 0) - (stats.recovered_value_paise ?? 0)

  return (
    <section className="headline">
      <div className="stat hero">
        <div className="k">revenue recovered</div>
        <div className="v">{rupeesShort(stats.recovered_value_paise)}</div>
        <div className="foot">
          {num(stats.recovered_count)} of {num(stats.failed_payments)} failed orders ·{' '}
          {pct(stats.recovery_rate_by_value)} by value · {rupees(stats.recovered_value_paise)}
        </div>
      </div>

      <div className="stat">
        <div className="k">still lost</div>
        <div className="v">{rupeesShort(outstanding)}</div>
        <div className="foot">
          {rupeesShort(stats.failed_value_paise)} failed in window · {num(stats.pending)} still
          in flight
        </div>
      </div>

      <div className="stat">
        <div className="k">attempts</div>
        <div className="v">{num(stats.attempts_made)}</div>
        <div className="foot">
          {pct(stats.attempt_conversion_rate)} converted ·{' '}
          {num(stats.customer_contacts)} customer messages
        </div>
      </div>

      <div className="stat">
        <div className="k">not attempted</div>
        <div className="v">{num(stats.suppressed)}</div>
        <div className="foot">
          held back by guardrails ·{' '}
          {sched
            ? `scheduler ${sched.running ? 'running' : 'idle'}, ${num(sched.ticks)} ticks`
            : 'scheduler unknown'}
        </div>
      </div>
    </section>
  )
}
