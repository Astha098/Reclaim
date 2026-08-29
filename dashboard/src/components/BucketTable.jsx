import { humanize, num, pct, rupeesShort } from '../fmt.js'

/**
 * Per-bucket results — the table that shows the taxonomy earning its keep.
 *
 * `pending` is a column rather than an afterthought. A bucket showing 40 failures,
 * 12 recovered and 0 pending is finished; the same bucket with 20 pending is a
 * mid-flight number, and reading it as final understates the system. This is the
 * single easiest way to accidentally lie in a recovery dashboard.
 */
export default function BucketTable({ buckets }) {
  const rows = [...(buckets ?? [])].sort(
    (a, b) => (b.recovered_value_paise ?? 0) - (a.recovered_value_paise ?? 0),
  )
  const best = Math.max(...rows.map((r) => r.recovered_value_paise ?? 0), 1)

  const tot = rows.reduce(
    (a, r) => ({
      failed_count: a.failed_count + r.failed_count,
      failed_value_paise: a.failed_value_paise + r.failed_value_paise,
      attempts: a.attempts + r.attempts,
      pending: a.pending + r.pending,
      suppressed: a.suppressed + r.suppressed,
      recovered: a.recovered + r.recovered,
      recovered_value_paise: a.recovered_value_paise + r.recovered_value_paise,
    }),
    {
      failed_count: 0,
      failed_value_paise: 0,
      attempts: 0,
      pending: 0,
      suppressed: 0,
      recovered: 0,
      recovered_value_paise: 0,
    },
  )

  return (
    <div className="panel span2">
      <h2>Failure buckets</h2>
      <p className="sub">
        Every failed payment lands in exactly one bucket, and the bucket picks the recovery
        action. Buckets are defined by what you can <i>do</i> about them, not by the error
        string the gateway happened to return.
      </p>

      {!rows.length ? (
        <div className="empty">No classified payments yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>bucket</th>
              <th className="n">failed</th>
              <th className="n">value</th>
              <th className="n">attempts</th>
              <th className="n">pending</th>
              <th className="n">held</th>
              <th className="n">won</th>
              <th className="n">recovered</th>
              <th className="n">conv.</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.bucket}>
                <td>{humanize(r.bucket)}</td>
                <td className="n">{num(r.failed_count)}</td>
                <td className="n">{rupeesShort(r.failed_value_paise)}</td>
                <td className="n">{num(r.attempts)}</td>
                <td className="n" style={{ color: r.pending ? 'var(--info)' : 'var(--dimmer)' }}>
                  {r.pending ? num(r.pending) : '—'}
                </td>
                <td
                  className="n"
                  style={{ color: r.suppressed ? 'var(--warn)' : 'var(--dimmer)' }}
                >
                  {r.suppressed ? num(r.suppressed) : '—'}
                </td>
                <td className="n">{num(r.recovered)}</td>
                <td className="n bar-cell">
                  <div
                    className="fill"
                    style={{ width: `${((r.recovered_value_paise ?? 0) / best) * 100}%` }}
                  />
                  <span>{rupeesShort(r.recovered_value_paise)}</span>
                </td>
                <td className="n">{r.attempts ? pct(r.conversion, 0) : '—'}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>total</td>
              <td className="n">{num(tot.failed_count)}</td>
              <td className="n">{rupeesShort(tot.failed_value_paise)}</td>
              <td className="n">{num(tot.attempts)}</td>
              <td className="n">{tot.pending ? num(tot.pending) : '—'}</td>
              <td className="n">{tot.suppressed ? num(tot.suppressed) : '—'}</td>
              <td className="n">{num(tot.recovered)}</td>
              <td className="n">{rupeesShort(tot.recovered_value_paise)}</td>
              <td className="n">
                {tot.attempts ? pct(tot.recovered / tot.attempts, 0) : '—'}
              </td>
            </tr>
          </tfoot>
        </table>
      )}

      <p className="note-line">
        <b>held</b> means a decision was made not to attempt — a risk decline, an
        uncontactable customer, an exhausted attempt budget. <b>pending</b> means scheduled
        and not yet due, so those rows are not losses yet.
      </p>
    </div>
  )
}
