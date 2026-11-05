// Which findings a review shows, as on the classic page: farm keys get only what hurt, push keys the full review.
import { lsGet } from "./wow"

export type Finding = {
  t_s: number; clock: string; pull: string; severity: number; title: string; short?: string; fix?: string
  detail: string[]; context: [string, string][]; rule: string
}

export type Strict = "auto" | "relaxed" | "normal" | "strict"
export type Tier = { key: "farm" | "normal" | "push"; line: string; minorWhy: string }

const OUTCOME_RULES = ["death", "self_danger"]  // deaths and near-deaths always show
const NITPICK_RULES = ["riptide_prep", "spell_choice", "buff_choice", "planned_cooldown"]
const STUDY_RULES = ["planned_cooldown", "riptide_prep", "cooldown_rate"]  // timings learned from top players

export const strictPick = (): Strict => (["relaxed", "normal", "strict"].includes(lsGet("strict") || "") ? lsGet("strict") as Strict : "auto")

export function reviewTier(level: number, best: number | null, pick: Strict): Tier {
  const lines = { farm: "Farm key - showing only what hurt", normal: "Normal key - what hurt and clear patterns",
                  push: "Push key - full review" }
  let key = ({ relaxed: "farm", normal: "normal", strict: "push" } as const)[pick as "relaxed"] as Tier["key"] | undefined
  let why = "set by you"
  if (!key) {
    const gap = best == null ? 0 : best - level
    key = gap >= 5 ? "farm" : gap >= 2 ? "normal" : "push"
    why = best == null ? "your best key isn't known" : `your best timed key is +${best}`
  }
  return { key, line: `${lines[key]} (${why}).`,
           minorWhy: key === "farm" ? "farm key" : key === "normal" ? "below your push range" : "top players' timings are from higher keys" }
}

// Timings learned from top players only count near the study's key range.
export function splitFindings(findings: Finding[], level: number, model: string | undefined, tier: Tier) {
  const studyMin = Math.min(...(model || "").split(",").map((x) => parseInt(x.replace("+", ""))).filter((n) => n > 0))
  const shown: Finding[] = [], minor: Finding[] = []
  for (const f of findings) {
    const outcome = OUTCOME_RULES.includes(f.rule) || f.severity >= 70
    const farFromStudy = STUDY_RULES.includes(f.rule) && Number.isFinite(studyMin) && level < studyMin - 2
    const keep = !farFromStudy && (tier.key === "push" || outcome
      || (tier.key === "normal" && f.severity >= 45 && !NITPICK_RULES.includes(f.rule)))
    ;(keep ? shown : minor).push(f)
  }
  return { shown, minor }
}

export type Sev = "high" | "med" | "low"
export const sevOf = (s: number): Sev => (s >= 70 ? "high" : s >= 45 ? "med" : "low")
export const SEV_LABEL: Record<Sev, string> = { high: "High", med: "Medium", low: "Low" }

// In run order, whole-run notes first.
export const byTime = (list: Finding[]) =>
  [...list].sort((a, b) => (a.pull === "Whole run" ? -1 : a.t_s) - (b.pull === "Whole run" ? -1 : b.t_s))

// "Pull 3: Melidrussa Chillworn (boss) · after Awaken Whelps" -> "Melidrussa (after Awaken Whelps)"
export function pullShort(label: string): string {
  if (!label || label === "Whole run") return ""
  const name = (label.split(": ")[1] || label).split(" (boss)")[0].split(" · ")[0]
  const part = label.split(" · ")[1]
  return name.split(" ")[0] + (part ? ` (${part})` : "")
}
