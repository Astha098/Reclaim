/**
 * The honesty banner.
 *
 * Sits above everything, permanently, and says in plain language that the
 * recovery outcomes on this screen are simulated. Two reasons it is not a
 * footnote:
 *
 * A judge who discovers halfway through that the numbers are synthetic stops
 * trusting the whole demo, and rightly so. Declaring it up front converts the
 * same fact from a catch into a design statement — the decision logic is real,
 * the API calls are real, the conversion draw is a model, and the model is
 * documented in `app/simulator.py`.
 *
 * It also flips to green automatically once real Razorpay keys are configured,
 * so nobody has to remember to delete the disclaimer before going live. A banner
 * you have to remember to remove is a banner that ships to production.
 */
export default function Banner({ config }) {
  if (!config) return null

  const mock = config.razorpay_mode === 'mock'
  if (!mock) {
    return (
      <div className="banner live">
        <strong>live keys</strong>
        <span>
          Talking to Razorpay with {config.razorpay_mode} credentials. Payment links and
          charges on this screen are real.
        </span>
      </div>
    )
  }

  return (
    <div className="banner">
      <strong>mock mode</strong>
      <span>
        Razorpay API is mocked and <b>recovery outcomes are simulated</b> — no real charge
        happens. Every decision, guardrail and circuit transition you see is the real code
        path; only the conversion draw is a model. Calibration and its limits are documented
        in <code>app/simulator.py</code> and the README.
      </span>
      <span style={{ color: '#fcd34d99' }}>
        classifier: {config.classifier_mode}
        {config.llm_available ? ` (${config.llm_model})` : ' (rules only — no API key set)'}
      </span>
    </div>
  )
}
