// The Python API (app/web.py) - same origin in production, proxied by Vite in development.

export class ApiError extends Error {
  status: number
  ref?: string
  constructor(message: string, status: number, ref?: string) {
    super(message)
    this.status = status
    this.ref = ref
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...init })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new ApiError(body.detail || `Request failed (${r.status})`, r.status, body.ref)
  return body as T
}

export type Me = { mode: "local" | "wcl"; user: string | null; user_id?: number | null; admin?: boolean }

export type Settings = { name?: string; server?: string; region?: string; class?: string }

export type Char = { name: string; server: string; region: string; level?: number; class?: string | null }

export type MyChars = { characters: Char[]; local?: boolean; relogin?: boolean; busy?: boolean }

export type Run = {
  url: string
  dungeon: string
  level: number
  timed: boolean
  duration_min: number
  started: string
  spec?: string
  analyzable: boolean
  analyzed: boolean
  queued?: boolean
}

export type Recent = { runs: Run[]; more?: boolean; partial?: string | null; note?: string; saved?: boolean }
