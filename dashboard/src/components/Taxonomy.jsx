import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { humanize, pct } from '../fmt.js'

/**
 * The policy table, read live from `/api/taxonomy`.
 *
 * Rendered from the endpoint rather than duplicated here on purpose. A dashboard
 * that hardcodes the policy it claims to be documenting will eventually document a
 * policy the system no longer runs, and nobody will notice — the two copies fail
 * silently and independently. This one cannot drift: change `app/taxonomy.py` and
 * this table changes with it.
 *
 * Collapsed by default because it is reference material, not a live signal.
 */
export default function Taxonomy() {
  const [rows, setRows] = useState(null)
  const [loaded, setLoaded] = useState(false)

  // Fetched on first expand rather than on mount — reference material should not
  // cost a request on a screen where nobody opened it.
  useEffect(() => {
    if (!loaded) return
    api.taxonomy().then(setRows).catch(() => setRows([]))
  }, [loaded])

  return (
    <details className="taxonomy" onToggle={(e) => e.currentTarget.open && setLoaded(true)}>
      <summary>How it decides — the policy table</summary>
      <div className="body">
        <p className="sub" style={{ marginTop: 12 }}>
          One row per failure bucket. The bucket determines the action, the timing, the
          preferred rail and how many attempts are permitted — and the attempt cap here is the
          real one the runtime enforces, not documentation of an intention.
        </p>
        {!rows ? (
          <div className="empty">Loading…</div>
        ) : !rows.length ? (
          <div className="empty">Could not load the policy table.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>bucket</th>
                <th>action</th>
                <th>timing</th>
                <th>rail</th>
                <th className="n">max</th>
                <th className="n">base rate</th>
                <th>contacts</th>
                <th>why</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.bucket}>
                  <td>
                    <b>{humanize(r.bucket)}</b>
                  </td>
                  <td>
                    <span className={`pill ${r.retryable ? 'info' : 'muted'}`}>
                      {humanize(r.action)}
                    </span>
                  </td>
                  <td style={{ color: 'var(--dim)' }}>{humanize(r.timing)}</td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: '11.5px' }}>
                    {r.preferred_rail}
                    {r.excluded_rails?.length ? ` −${r.excluded_rails.join(',')}` : ''}
                  </td>
                  <td className="n">{r.max_attempts}</td>
                  <td className="n" style={{ color: 'var(--dim)' }}>
                    {pct(r.base_recovery_rate, 0)}
                  </td>
                  <td>
                    <span className={`pill ${r.contacts_customer ? 'warn' : 'muted'}`}>
                      {r.contacts_customer ? 'yes' : 'silent'}
                    </span>
                  </td>
                  <td className="rationale">{r.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="note-line">
          <b>base rate</b> is the prior this bucket's recovery attempts are modelled against in
          mock mode, before timing, attempt number and amount adjust it. It is an assumption,
          sourced and argued in <code>app/simulator.py</code> — not a measurement.
        </p>
      </div>
    </details>
  )
}
