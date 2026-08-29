/**
 * API client.
 *
 * Every path is relative so the dev proxy in `vite.config.js` handles it and a
 * production build can be served from the FastAPI app itself with no change.
 * Point `VITE_API_BASE` at a full origin to talk to a remote instance instead.
 */

const BASE = import.meta.env.VITE_API_BASE ?? ''

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    // Surface the server's own message. A recovery system whose dashboard says
    // "something went wrong" is not a useful recovery system.
    const detail = await res.text().catch(() => '')
    throw new Error(`${method} ${path} → ${res.status} ${detail.slice(0, 300)}`)
  }
  return res.json()
}

export const api = {
  config: () => request('/api/config'),
  stats: () => request('/api/stats'),
  buckets: () => request('/api/buckets'),
  attempts: (limit = 60) => request(`/api/attempts?limit=${limit}`),
  issuers: () => request('/api/issuers'),
  classifier: () => request('/api/classifier'),
  suppressions: () => request('/api/suppressions'),
  timeline: () => request('/api/timeline'),
  scheduler: () => request('/api/scheduler'),
  taxonomy: () => request('/api/taxonomy'),

  seed: (body) => request('/api/demo/seed', { method: 'POST', body }),
  tick: (limit = 200) => request('/api/demo/tick', { method: 'POST', body: { limit } }),
  reset: () => request('/api/demo/reset', { method: 'POST', body: {} }),
  outage: (issuer = 'HDFC', rail = 'card') =>
    request('/api/demo/outage', { method: 'POST', body: { issuer, rail } }),
  recoverIssuer: (issuer = 'HDFC', rail = 'card') =>
    request('/api/demo/recover-issuer', { method: 'POST', body: { issuer, rail } }),
}
