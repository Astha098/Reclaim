import { hourLabel, num, pct, rupeesShort } from '../fmt.js'

/**
 * Failed vs recovered value per hour.
 *
 * Hand-drawn SVG rather than a charting library. One bar per hour showing the
 * value that failed, with the recovered portion filled from the baseline, so the
 * shape of the chart answers "how much of what broke did we get back" without a
 * legend lookup. A second series would need axes reconciled and would say less.
 *
 * No chart dependency is a deliberate choice, not laziness: Recharts and friends
 * pull in a few hundred kilobytes and a d3 subtree to draw eleven rectangles, and
 * the install has to work first time on a reviewer's laptop.
 */
export default function Timeline({ timeline }) {
  const rows = timeline ?? []
  if (!rows.length) {
    return (
      <div className="panel span2">
        <h2>Recovery over time</h2>
        <p className="sub">Value that failed each hour, and how much of it came back.</p>
        <div className="empty">No traffic yet — seed a batch to populate the window.</div>
      </div>
    )
  }

  const W = 1000
  const H = 200
  const padL = 54
  const padR = 8
  const padT = 14
  const padB = 22
  const plotW = W - padL - padR
  const plotH = H - padT - padB

  const peak = Math.max(...rows.map((r) => r.failed_value_paise ?? 0), 1)
  const slot = plotW / rows.length
  const barW = Math.min(slot * 0.62, 48)

  const y = (paise) => padT + plotH - (paise / peak) * plotH

  const totalFailed = rows.reduce((a, r) => a + (r.failed_value_paise ?? 0), 0)
  const totalRecovered = rows.reduce((a, r) => a + (r.recovered_value_paise ?? 0), 0)

  return (
    <div className="panel span2">
      <h2>Recovery over time</h2>
      <p className="sub">
        Value that failed each hour, and how much of it came back. Recovery lands in later
        buckets than the failure that caused it — a payday-aligned retry is supposed to.
      </p>

      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`Hourly failed and recovered value. ${rupeesShort(totalRecovered)} recovered of ${rupeesShort(totalFailed)} failed.`}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line className="grid-line" x1={padL} x2={W - padR} y1={y(peak * f)} y2={y(peak * f)} />
            <text className="axis" x={padL - 8} y={y(peak * f) + 3.5} textAnchor="end">
              {f === 0 ? '0' : rupeesShort(peak * f)}
            </text>
          </g>
        ))}

        {rows.map((r, i) => {
          const cx = padL + slot * i + slot / 2
          const x = cx - barW / 2
          const failed = r.failed_value_paise ?? 0
          const recovered = r.recovered_value_paise ?? 0
          const rate = failed ? recovered / failed : 0
          return (
            <g key={r.hour ?? i}>
              <title>
                {`${hourLabel(r.hour)} — ${num(r.failed)} failed (${rupeesShort(failed)}), ` +
                  `${num(r.recovered)} recovered (${rupeesShort(recovered)}, ${pct(rate, 0)})`}
              </title>
              <rect
                x={x}
                y={y(failed)}
                width={barW}
                height={Math.max(padT + plotH - y(failed), 1)}
                fill="#f8717126"
                stroke="#f8717155"
                rx="3"
              />
              {recovered > 0 && (
                <rect
                  x={x}
                  y={y(recovered)}
                  width={barW}
                  height={Math.max(padT + plotH - y(recovered), 1)}
                  fill="#34d39933"
                  stroke="#34d399"
                  rx="3"
                />
              )}
              {/* Label every hour when there is room, otherwise every other one. */}
              {(rows.length <= 12 || i % 2 === 0) && (
                <text className="axis" x={cx} y={H - 6} textAnchor="middle">
                  {hourLabel(r.hour)}
                </text>
              )}
            </g>
          )
        })}
      </svg>

      <div className="legend">
        <span>
          <i style={{ background: '#f8717126', border: '1px solid #f8717155' }} />
          failed {rupeesShort(totalFailed)}
        </span>
        <span>
          <i style={{ background: '#34d39933', border: '1px solid #34d399' }} />
          recovered {rupeesShort(totalRecovered)}
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--dimmer)' }}>
          hover a bar for the hour's detail
        </span>
      </div>
    </div>
  )
}
