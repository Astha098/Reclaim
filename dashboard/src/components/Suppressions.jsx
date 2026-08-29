import { humanize, num, pct, rupeesShort } from '../fmt.js'

const EXPLAIN = {
  not_retryable:
    'Bucket is never retried — a risk decline or an unclassifiable failure. Retrying these buys chargebacks, not revenue.',
  no_contact_channel:
    'No phone and no email on the payment, so there is nobody to send a recovery link to.',
  attempt_budget_exhausted: 'Already attempted the maximum this bucket allows.',
  cooldown: 'Too soon after the previous attempt on this order.',
  quiet_hours: 'Would have messaged a customer during quiet hours.',
  customer_contact_budget: 'Customer has already had the maximum messages for today.',
  merchant_contact_budget: 'Merchant-wide daily message ceiling reached.',
  customer_suppressed: 'Customer previously opted out of recovery messages.',
  order_already_paid: 'Order was paid by another route — attempting again risks a double charge.',
  attempt_in_flight: 'An attempt on this order is already scheduled or running.',
}

/**
 * What the agent deliberately did *not* do.
 *
 * This panel is the point of the project as much as the recovered-revenue number
 * is. Any script can retry a failed payment; the reason a payments company can
 * actually deploy this one is that it declines to retry, on the record, with a
 * reason attached — and that restraint is worth showing rather than hiding.
 *
 * Value is shown alongside count because the two tell different stories: dropping
 * four uncontactable customers worth ₹2.3L is a bigger deal than dropping
 * twenty-eight risk declines worth ₹28k, and a count-only view inverts that.
 */
export default function Suppressions({ suppressions, stats }) {
  const rows = suppressions ?? []
  const total = rows.reduce((a, r) => a + (r.count ?? 0), 0)
  const value = rows.reduce((a, r) => a + (r.value_paise ?? 0), 0)
  const share = stats?.failed_payments ? total / stats.failed_payments : 0

  return (
    <div className="panel">
      <h2>Deliberately not attempted</h2>
      <p className="sub">
        {total
          ? `${num(total)} of ${num(stats?.failed_payments ?? 0)} failed payments (${pct(share, 0)}, ${rupeesShort(value)}) were held back on purpose.`
          : 'Nothing has been held back yet.'}
      </p>

      {!rows.length ? (
        <div className="empty">No suppressions recorded.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>reason</th>
              <th className="n">count</th>
              <th className="n">value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.reason}>
                <td title={EXPLAIN[r.reason] ?? ''}>
                  <span className="pill warn">{humanize(r.reason)}</span>
                </td>
                <td className="n">{num(r.count)}</td>
                <td className="n">{rupeesShort(r.value_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {rows.length > 0 && (
        <div className="note-line">
          {rows.map((r) => (
            <div key={r.reason} style={{ marginTop: 6 }}>
              <b style={{ color: 'var(--dim)' }}>{humanize(r.reason)}</b> —{' '}
              {EXPLAIN[r.reason] ?? 'Suppressed by a guardrail.'}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
