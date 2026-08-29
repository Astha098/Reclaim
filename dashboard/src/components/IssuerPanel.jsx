import { clock, num, pct } from '../fmt.js'

const TONE = { closed: 'good', half_open: 'warn', open: 'bad' }

/**
 * Per-(rail, issuer) circuit state.
 *
 * `opened_count` is shown next to the current state because the state alone is a
 * snapshot: an issuer that flapped six times in the last hour and happens to be
 * closed right now reads identically to one that has been healthy all day, and
 * those are very different operational facts.
 *
 * Sorted worst-first by the backend, so an outage puts itself at the top of the
 * panel without the operator going looking for it.
 */
export default function IssuerPanel({ issuers, circuitCfg }) {
  const rows = issuers ?? []
  const open = rows.filter((r) => r.state === 'open').length
  const probing = rows.filter((r) => r.state === 'half_open').length

  return (
    <div className="panel">
      <h2>Issuer circuits</h2>
      <p className="sub">
        {open || probing ? (
          <>
            <b style={{ color: 'var(--bad)' }}>{open} open</b>
            {probing ? `, ${probing} probing` : ''} — retries to these are held or steered to
            another rail.
          </>
        ) : (
          'All circuits closed. Retries flow normally.'
        )}
      </p>

      {!rows.length ? (
        <div className="empty">No authorization traffic observed yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>rail : issuer</th>
              <th className="n">success</th>
              <th className="n">n</th>
              <th>state</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.issuer_key}>
                <td style={{ fontFamily: 'var(--mono)', fontSize: '12px' }}>{r.issuer_key}</td>
                <td className="n">{r.attempts ? pct(r.success_rate, 0) : '—'}</td>
                <td className="n" style={{ color: 'var(--dimmer)' }}>
                  {num(r.attempts)}
                </td>
                <td>
                  <span className={`pill ${TONE[r.state] ?? 'muted'}`}>
                    {r.state === 'half_open'
                      ? `probe ${r.probes_used}/${circuitCfg?.probe_attempts ?? '?'}`
                      : r.state}
                  </span>
                  {r.opened_count > 0 && (
                    <span
                      className="pill muted"
                      style={{ marginLeft: 5 }}
                      title={
                        `Opened ${r.opened_count}× since start` +
                        (r.opened_at ? `, last at ${clock(r.opened_at)}` : '')
                      }
                    >
                      ×{r.opened_count}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {circuitCfg && (
        <p className="note-line">
          Opens below {pct(circuitCfg.open_below, 0)} success over ≥{circuitCfg.min_samples}{' '}
          attempts in {circuitCfg.window_minutes}m; probes after{' '}
          {circuitCfg.half_open_after_minutes}m; closes above {pct(circuitCfg.close_above, 0)}.
          The <code>min_samples</code> floor is what stops a low-volume bank from tripping on
          noise.
        </p>
      )}
    </div>
  )
}
