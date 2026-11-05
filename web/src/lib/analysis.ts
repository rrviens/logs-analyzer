import { useCallback, useEffect, useRef, useState } from "react"
import { api, ApiError } from "./api"
import type { Finding } from "./review"
import type { Death } from "@/components/Deaths"
import type { Verdict } from "@/components/Sections"
import type { TimelineData } from "@/components/Timeline"

// The parts of an analysis result the new views use so far (the API sends more).
export type Bring = { name: string; scope: "group" | "external"; casts: number; on_big_hits: number }
export type Member = { name: string; role: string; spec?: string; class?: string; note?: string; brings?: Bring[]; big_hits?: number }
export type Meter = { name: string; class: string; spec: string; total: number; per_s: number }
type HealStats = { casts: number; ch_share?: number; ch_overheal?: number; hw_overheal?: number; ch_effective?: number
                  hw_effective?: number; mana?: number }
export type Analysis = {
  run: { dungeon: string; level: number; timed: boolean; duration_min: number; deaths: number; player: string }
  spec: { key: string; label: string; plural: string; single?: string; group?: string; majors?: string }
  compared?: { count: number; levels: number[]; model?: string; skipped?: { player: string; key: number; reasons: string[] }[] }
  group: Member[]
  meters?: { damage: Meter[]; healing: Meter[] } | null
  comp_notes?: string[]
  findings?: Finding[]
  deaths?: Death[]
  verdicts?: { items?: Verdict | null; spells?: Verdict | null; pulls?: Verdict | null }
  items?: { name: string; kind: string; uses: number; top_uses: number | null; top_users: number; top_count: number
            ready_pressure_s: number | null; paired: number | null; top_paired: number | null }[]
  spell_choice?: { you?: Record<string, HealStats>; top?: Record<string, HealStats>
                   buffs?: { you: Record<string, HealStats>; top?: Record<string, HealStats> } } | null
  dps?: { windows: number; seconds: number; your_dps?: number; top_dps?: number; missed?: number } | null
  timeline?: TimelineData & {
               dps_windows?: { clock: string; pull: string; start_s: number; end_s: number; seconds: number; use: number | null
                               missed: number; dps_casts: number; other_casts: number; idle_s: number; tip: string[] }[] }
  requirements?: { pull: string; duration_s: number; group_hp: number; top_group_hp: number | null; intake: number
                   top_intake: number | null; healing: number; top_healing: number | null }[]
  phases?: { pull: string; phase: string; clock: string; duration_s: number; group_hp: number; top_group_hp: number | null
             intake: number; top_intake: number | null }[]
  good_calls?: { title: string; pull: string; detail: string[] }[]
  preview?: unknown
}

type Job = { status: "queued" | "running" | "waiting" | "done" | "error"; progress: string[]; note?: string; error?: string;
             position?: number; result?: Analysis; preview?: Analysis & { stage?: number } }
type Start = { result?: Analysis; job?: string; stale?: boolean }

export type AnalysisState = {
  result: Analysis | null      // full result, or the saved one while it updates
  preview: Analysis | null     // your run alone, while top logs download
  progress: string[]
  updating: string             // a saved result is shown while a newer one is made
  error: string
}

// Opens a key: a saved analysis on the current rules shows at once; otherwise a job is queued and polled
// (preview first, then the full result). Re-analyze runs it again from scratch.
export function useAnalysis(url: string | null) {
  const [s, set] = useState<AnalysisState>({ result: null, preview: null, progress: [], updating: "", error: "" })
  const current = useRef(url)

  const run = useCallback(async (fresh = false) => {
    if (!url) return
    current.current = url
    set((p) => ({ ...p, error: "", progress: ["Starting..."], ...(fresh ? {} : { result: null, preview: null, updating: "" }) }))
    try {
      const r = await api<Start>("/api/analyze", { method: "POST", body: JSON.stringify({ url, fresh }) })
      if (r.result && !r.job) return set((p) => ({ ...p, result: r.result!, updating: "", progress: [] }))
      if (r.result) set((p) => ({ ...p, result: r.result!, updating: "Updating to the latest rules..." }))
      let hasPreview = false
      while (current.current === url) {
        await new Promise((res) => setTimeout(res, 1500))
        if (current.current !== url) return
        const j = await api<Job>(`/api/jobs/${r.job}${hasPreview || r.result ? "?skip_preview=true" : ""}`)
        if (j.status === "done") return set((p) => ({ ...p, result: j.result!, preview: null, updating: "", progress: [] }))
        if (j.status === "error") {
          if (r.result) return set((p) => ({ ...p, updating: `Couldn't update to the latest rules (${j.error}) - showing the saved analysis.` }))
          throw new ApiError(j.error || "Analysis failed", 500)
        }
        const status = j.status === "queued" ? `Queued${j.position ? ` (${j.position} ahead)` : ""}...` : j.status === "waiting" ? j.note || "" : ""
        if (j.preview && !hasPreview) hasPreview = true
        set((p) => ({ ...p, preview: j.preview ?? p.preview, progress: status ? [status] : j.progress,
                      updating: r.result ? status || "Updating to the latest rules..." : "" }))
      }
    } catch (e) {
      if (current.current === url) set((p) => ({ ...p, error: e instanceof Error ? e.message : String(e), progress: [] }))
    }
  }, [url])

  useEffect(() => { run() }, [run])
  return { ...s, reanalyze: () => run(true) }
}
