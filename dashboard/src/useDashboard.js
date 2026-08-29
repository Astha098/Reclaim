import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'

/**
 * One hook that owns every read model, polled together.
 *
 * Polling rather than websockets on purpose. The whole dataset is a handful of
 * small aggregates over a local SQLite file, a full refresh costs single-digit
 * milliseconds, and a websocket would add a reconnect state machine to a demo
 * that gains nothing from it. If this were serving many operators the calculus
 * changes; at one operator and a 2.5s cadence it does not.
 *
 * `inFlight` guards against overlapping polls: a slow response must not stack up
 * a queue of requests behind it, because the visible symptom of that is a
 * dashboard that lurches between two versions of the past.
 */
export function useDashboard({ intervalMs = 2500 } = {}) {
  const [data, setData] = useState(null)
  const [config, setConfig] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState(true)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [stats, buckets, attempts, issuers, classifier, suppressions, timeline, sched] =
        await Promise.all([
          api.stats(),
          api.buckets(),
          api.attempts(60),
          api.issuers(),
          api.classifier(),
          api.suppressions(),
          api.timeline(),
          api.scheduler(),
        ])
      setData({ stats, buckets, attempts, issuers, classifier, suppressions, timeline, sched })
      setError(null)
    } catch (e) {
      setError(e.message ?? String(e))
    } finally {
      inFlight.current = false
    }
  }, [])

  // Config and the taxonomy are static for the process lifetime, so they are
  // fetched once rather than on every poll.
  useEffect(() => {
    api
      .config()
      .then(setConfig)
      .catch((e) => setError(e.message ?? String(e)))
    refresh()
  }, [refresh])

  useEffect(() => {
    if (!live) return undefined
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [live, intervalMs, refresh])

  /**
   * Run a demo action, then refresh immediately.
   *
   * Returns the action's own response so the caller can show what happened —
   * `recover-issuer` reports the path the circuit took, and swallowing that
   * would hide the most interesting thing the button does.
   */
  const act = useCallback(
    async (fn) => {
      setBusy(true)
      try {
        const result = await fn()
        await refresh()
        setError(null)
        return result
      } catch (e) {
        setError(e.message ?? String(e))
        return null
      } finally {
        setBusy(false)
      }
    },
    [refresh],
  )

  return { data, config, error, busy, live, setLive, refresh, act }
}
