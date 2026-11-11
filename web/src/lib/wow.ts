import type { Run } from "./api"

export const CLASS_COLORS: Record<string, string> = {
  "Death Knight": "#C41E3A", "Demon Hunter": "#A330C9", Druid: "#FF7C0A", Evoker: "#33937F",
  Hunter: "#AAD372", Mage: "#3FC7EB", Monk: "#00FF98", Paladin: "#F48CBA", Priest: "#B8B8B8",
  Rogue: "#E6D02E", Shaman: "#0070DD", Warlock: "#8788EE", Warrior: "#C69B6D",
  DeathKnight: "#C41E3A", DemonHunter: "#A330C9",  // WCL's spelling, in meters saved before the name was spaced
}

// Warcraft Logs' parse colours: grey, green, blue, purple, orange, pink (99), gold (100).
export const parseColor = (p: number) =>
  p >= 100 ? "#E5CC80" : p >= 99 ? "#E268A8" : p >= 95 ? "#FF8000" : p >= 75 ? "#A335EE" : p >= 50 ? "#0070FF" : p >= 25 ? "#1EFF00" : "#9D9D9D"

// A key that ended untimed this short was left before the end (a completed key runs longer).
const ABANDONED_MIN = 20
export type Result = "timed" | "untimed" | "abandoned"
export const result = (r: Run): Result => (r.timed ? "timed" : r.duration_min < ABANDONED_MIN ? "abandoned" : "untimed")
export const RESULT_LABEL: Record<Result, string> = { timed: "Timed", untimed: "Not timed", abandoned: "Abandoned" }

export function ago(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 60) return `${mins}m ago`
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`
  return new Date(iso).toLocaleDateString()
}

// Keys played less than an hour apart (end of one to start of the next) are one play session, across reports.
const SESSION_GAP_MS = 3600 * 1000
export type Session = { id: string; day: string; hours: string; count: string; timed: number; runs: Run[] }

export function playSessions(runs: Run[]): Session[] {
  const sorted = [...runs].sort((a, b) => +new Date(b.started) - +new Date(a.started))
  const out: { runs: Run[]; start: number; end: number }[] = []
  for (const r of sorted) {
    const start = +new Date(r.started), end = start + r.duration_min * 60000, s = out[out.length - 1]
    if (s && s.start - end < SESSION_GAP_MS) {
      s.runs.push(r)
      s.start = start
    } else out.push({ runs: [r], start, end })
  }
  const hm = (t: number) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
  return out.map((s) => {
    const day = new Date(s.start).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
    const timed = s.runs.filter((r) => r.timed).length, n = s.runs.length
    return { id: new Date(s.start).toISOString(), runs: s.runs, day, hours: `${hm(s.start)}–${hm(s.end)}`,
             count: `${n} key${n > 1 ? "s" : ""}`, timed }
  })
}

// Shared with the classic page (same keys), so preferences carry over both ways.
export function lsGet(k: string): string | null {
  try { return localStorage.getItem(k) } catch { return null }
}
export function lsSet(k: string, v: string): void {
  try { localStorage.setItem(k, v) } catch { /* private mode: not remembered */ }
}
