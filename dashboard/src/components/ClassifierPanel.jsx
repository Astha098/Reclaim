import { num, pct } from '../fmt.js'

const LABEL = {
  rules: 'rules',
  llm: 'LLM',
  llm_fallback_rules: 'LLM unavailable → rules',
  abstained: 'abstained',
}

const EXPLAIN = {
  rules: 'Deterministic match on error_reason/error_code. Free, instant, auditable — the rule that fired is recorded on the classification.',
  llm: 'The residue the rules could not place: vague prose, missing error_reason. Costs a call, so it only runs where it earns one.',
  llm_fallback_rules:
    'The LLM call failed or no API key was configured, so the rules result was used instead. Classification degrades; it does not stop.',
  abstained:
    'Neither route reached the confidence floor, so the payment was left unclassified and never charged. Guessing here means retrying a fraud decline.',
}

const TONE = { rules: 'good', llm: 'info', llm_fallback_rules: 'warn', abstained: 'muted' }

/**
 * Where classifications came from.
 *
 * The interesting number is the rules share. A hybrid classifier that sends every
 * payment to an LLM is a cost centre with a latency problem; one that resolves
 * ~90% on deterministic rules and spends the model only on genuine ambiguity is
 * the version you can put in front of production traffic. That ratio is the claim
 * this panel exists to substantiate.
 *
 * The abstention row matters just as much: a classifier permitted to say "I don't
 * know" is what keeps an unclassifiable failure from being retried as if it were
 * a low balance.
 */
export default function ClassifierPanel({ classifier, config }) {
  const rows = classifier?.by_source ?? []
  const rules = rows.find((r) => r.source === 'rules')

  return (
    <div className="panel">
      <h2>Classifier</h2>
      <p className="sub">
        {rules
          ? `${pct(rules.share, 0)} of ${num(classifier.total)} payments resolved on deterministic rules; the LLM is reserved for what rules cannot place.`
          : 'No classifications yet.'}
      </p>

      {!rows.length ? (
        <div className="empty">No classifications recorded.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>source</th>
              <th className="n">n</th>
              <th className="n">share</th>
              <th className="n">conf.</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.source}>
                <td title={EXPLAIN[r.source] ?? ''}>
                  <span className={`pill ${TONE[r.source] ?? 'muted'}`}>
                    {LABEL[r.source] ?? r.source}
                  </span>
                </td>
                <td className="n">{num(r.count)}</td>
                <td className="n">{pct(r.share, 0)}</td>
                <td className="n" style={{ color: 'var(--dim)' }}>
                  {r.avg_confidence ? r.avg_confidence.toFixed(2) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="note-line">
        Mode <code>{config?.classifier_mode ?? '—'}</code>
        {config?.llm_available
          ? ` · ${config.llm_model}`
          : ' · no ANTHROPIC_API_KEY set, so the LLM path is inert and rules carry everything'}
        . Abstentions are never charged.
      </p>
    </div>
  )
}
