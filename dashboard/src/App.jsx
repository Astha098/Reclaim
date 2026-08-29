import ActivityFeed from './components/ActivityFeed.jsx'
import Banner from './components/Banner.jsx'
import BucketTable from './components/BucketTable.jsx'
import ClassifierPanel from './components/ClassifierPanel.jsx'
import DemoControls from './components/DemoControls.jsx'
import Headline from './components/Headline.jsx'
import IssuerPanel from './components/IssuerPanel.jsx'
import Suppressions from './components/Suppressions.jsx'
import Taxonomy from './components/Taxonomy.jsx'
import Timeline from './components/Timeline.jsx'
import { useDashboard } from './useDashboard.js'

export default function App() {
  const { data, config, error, busy, live, setLive, refresh, act } = useDashboard()

  return (
    <div className="app">
      <Banner config={config} />

      <header className="top">
        <div className="brand">
          <h1>
            Re<span>claim</span>
          </h1>
          <p>
            Failed payments are not lost payments. This agent reads why each one failed, then
            picks the recovery action, the timing and the rail — and declines to attempt the
            ones that should not be attempted.
          </p>
        </div>
        <DemoControls act={act} busy={busy} live={live} setLive={setLive} refresh={refresh} />
      </header>

      {error && (
        <div className="error">
          {error}
          <br />
          <span style={{ color: 'var(--dimmer)' }}>
            Is the backend running? <code>uvicorn app.main:app --reload --port 8000</code>
          </span>
        </div>
      )}

      {!data ? (
        <div className="panel">
          <div className="empty">Connecting to the recovery engine…</div>
        </div>
      ) : (
        <>
          <Headline stats={data.stats} sched={data.sched} />

          <div className="grid">
            <Timeline timeline={data.timeline} />
            <IssuerPanel issuers={data.issuers} circuitCfg={config?.circuit} />

            <BucketTable buckets={data.buckets} />
            <Suppressions suppressions={data.suppressions} stats={data.stats} />

            <ActivityFeed attempts={data.attempts} />
            <ClassifierPanel classifier={data.classifier} config={config} />
          </div>

          <Taxonomy />
        </>
      )}

      <footer className="foot">
        <b style={{ color: 'var(--dim)' }}>What is real and what is modelled.</b> The webhook
        ingest, signature verification, classification, policy decisions, guardrails, circuit
        breaker, scheduling and idempotency are all production code paths — the same functions
        run in mock mode and against live keys. What mock mode simulates is the{' '}
        <i>outcome</i> of each attempt: whether a customer paid. That draw comes from{' '}
        <code>app/simulator.py</code>, whose per-bucket priors are stated and argued rather than
        tuned to flatter the agent.
        <br />
        <br />
        The headline uplift is measured separately in <code>eval/replay.py</code>, as a
        seven-rung ablation where each rung adds exactly one mechanism, over multiple seeds with
        common random numbers. A single agent-vs-naive number would not tell you which mechanism
        earned the money — and would hide the rung where the guardrails cost some of it back.
        {config?.guardrails && (
          <>
            <br />
            <br />
            <span style={{ color: 'var(--dim)' }}>
              Live limits: max {config.guardrails.max_attempts_per_order} attempts per order ·{' '}
              {config.guardrails.min_cooldown_minutes}m minimum cooldown · quiet hours{' '}
              {config.guardrails.quiet_hours[0]}:00–{config.guardrails.quiet_hours[1]}:00{' '}
              {config.timezone} · max {config.guardrails.max_contacts_per_customer_per_day}{' '}
              messages per customer per day.
            </span>
          </>
        )}
      </footer>
    </div>
  )
}
