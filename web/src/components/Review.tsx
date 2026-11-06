import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import { useAnalysis } from "@/lib/analysis"
import { Deaths } from "@/components/Deaths"
import { Findings } from "@/components/Findings"
import { Checks, DpsWindows } from "@/components/Sections"
import { Group } from "@/components/Group"
import { RunHeader } from "@/components/RunHeader"
import { buttonVariants } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { TimelineHandle } from "@/components/Timeline"
import { spellPattern, TimelineContext } from "@/lib/timeline"
import type { Analysis } from "@/lib/analysis"
import type { TPull } from "@/components/Timeline"

// The chart library is big: the timeline loads after the rest of the review.
const Timeline = lazy(() => import("@/components/Timeline").then((m) => ({ default: m.Timeline })))

// One key's review: header and group, timeline, then the sections.
// saved: a finished analysis to show read-only (signed out, from a shared link) - nothing is fetched or re-run.
export type LiveStatus = "analyzing" | "done" | "left"

export function Review({ url, saved, onStatus }: { url: string; saved?: Analysis; onStatus?: (s: LiveStatus) => void }) {
  const a = useAnalysis(saved ? null : url)
  const result = saved || a.result
  const analyzing = !saved && !a.error && (!a.result || !!a.updating)
  useEffect(() => { onStatus?.(analyzing ? "analyzing" : a.result ? "done" : "left") }, [analyzing, !!a.result])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => onStatus?.("left"), [])  // eslint-disable-line react-hooks/exhaustive-deps
  const shown = result || a.preview
  const [lookPull, setLookPull] = useState<TPull | null>(null)  // the pull open on the timeline filters "What to look at"
  const tl = useRef<TimelineHandle>(null)
  // A link's extra parameters (?run=<this key>&pull=..&view=..&base=..&at=..) apply to the key it names only.
  const deep = useMemo(() => { const q = new URLSearchParams(location.search); return q.get("run") === url ? q : null }, [url])
  const at = deep?.get("at") || null
  const CHECK_AT: Record<string, string> = { "sec-items": "items", "sec-spells": "spells", "sec-pulls": "pulls", "sec-good": "good" }
  useEffect(() => {  // &at=<section>: scroll there once the full result is in
    if (!at || !result) return
    const id = CHECK_AT[at] ? "sec-checks" : at
    const go = () => document.getElementById(id)?.scrollIntoView({ block: "start" })
    const timers = [300, 1500].map((ms) => setTimeout(go, ms))  // again once the timeline (loaded later) has settled the layout
    return () => timers.forEach(clearTimeout)
  }, [at, !!result])  // eslint-disable-line react-hooks/exhaustive-deps
  const spells = useMemo(() => spellPattern(result?.spell_ids), [result])
  const ctx = useMemo(() => ({ focus: (s: number, e?: number | null) => tl.current?.focus(s, e), spells }), [spells])
  const [best, setBest] = useState<number | null>(null)  // your best timed key (Raider.io): sets how strict the review is
  useEffect(() => {
    if (!saved) api<{ best: number | null }>(`/api/best?url=${encodeURIComponent(url)}`).then((d) => setBest(d.best ?? null)).catch(() => {})
  }, [url, saved])
  if (a.error && !shown) {
    return (
      <div className="rounded-xl border bg-card p-6">
        <h2 className="mb-1 text-lg font-semibold">Couldn't analyze this run</h2>
        <p className="mb-4 text-sm text-muted-foreground">{a.error}</p>
        <button className={buttonVariants({ variant: "outline", size: "sm" })} onClick={a.reanalyze}>Try again</button>
      </div>
    )
  }
  if (!shown) {
    return (
      <div className="flex flex-col gap-3 rounded-xl border bg-card p-6">
        <h2 className="text-lg font-semibold">Analyzing</h2>
        {a.progress.map((p, i) => <p key={i} className="text-sm text-muted-foreground">{p}</p>)}
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  const busy = !result || !!a.updating
  return (
    <TimelineContext.Provider value={ctx}>
    {/* Wide review area (not window): meters beside the header from ~56rem, a side column for the short sections
        from ~72rem - measured on the review itself, so it follows the key list being open or folded. */}
    <div className="square-ui @container flex flex-col gap-8">
      {(a.updating || (!result && a.progress.length > 0)) && (
        <p className="rounded-lg border bg-secondary px-3 py-2 text-sm text-muted-foreground">
          {a.updating || `Your run is ready; comparing with peers... ${a.progress.at(-1) || ""}`}
        </p>
      )}
      {result && <SectionBar res={result} />}
      <section className="grid gap-x-10 gap-y-6 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
        <div className="flex min-w-0 flex-col gap-5">
          <RunHeader res={shown} url={url} onReanalyze={saved ? undefined : a.reanalyze} busy={busy} />
          <Group res={shown} part="people" />
        </div>
        <Group res={shown} part="meters" />
      </section>
      {shown.timeline?.pulls?.length ? <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}><Timeline ref={tl} res={shown} onPull={setLookPull} deep={deep} /></Suspense> : null}
      {result && <Checks res={result} initial={at && CHECK_AT[at]} />}
      <div className="flex flex-col gap-8">
      {result?.findings ? (
        <Findings findings={result.findings} level={result.run.level} model={result.compared?.model} best={best}
                  pull={lookPull} onShowAll={() => setLookPull(null)} player={result.run.player}
                  deathFor={(f) => (f.rule === "death" && result.deaths?.find((d) => d.clock === f.clock && f.title.includes(d.victim))) || null} />
      ) : (
        <p className="text-sm text-muted-foreground">Findings appear when the comparison with your peers is done.</p>
      )}
      {result?.deaths && <Deaths deaths={result.deaths} player={result.run.player} classes={Object.fromEntries(result.group.map((m) => [m.name, m.class]))}
                                   findingFor={(d) => result.findings?.find((f) => f.rule === "death" && f.clock === d.clock && f.title.includes(d.victim)) || null} />}
      </div>
      {result && <DpsWindows res={result} />}
      {result && (
          <p className="text-xs text-muted-foreground">
            {result.encounter?.covered ? "Ability notes are our own, written from log evidence across top runs."
              : "No ability notes for this dungeon yet: everything here is read from combat logs, not the encounter journal - if a mechanic has a side effect the log can't show (e.g. dispelling something is punished), the advice changes."}
            {" "}Defensive values in data/defensives.json are approximate.
          </p>
      )}
    </div>
    </TimelineContext.Provider>
  )
}

// A sticky row of section links with their counts, to move around a long review (as on the classic page).
function SectionBar({ res }: { res: Analysis }) {
  const todo = (v?: { lines?: { action: boolean }[] } | null) => v?.lines?.filter((l) => l.action).length || 0
  const checks = todo(res.verdicts?.items) + todo(res.verdicts?.spells) + todo(res.verdicts?.pulls)
  const links: [string, string, number | null][] = [
    ["sec-timeline", "Timeline", null], ["sec-checks", "Other checks", checks || null],
    ["sec-look", "What to look at", res.findings?.length ?? null], ["sec-deaths", "Death recaps", res.deaths?.length ?? null],
    ["sec-dps", "DPS windows", (res.timeline?.dps_windows || []).filter((w) => w.use != null && w.use < 0.4).length || null],
  ]
  return (
    <nav aria-label="Sections" className="sticky top-14 z-20 -mx-1 order-first flex flex-wrap gap-1 border-b bg-background/95 px-1 py-2 backdrop-blur">
      {links.map(([id, label, n]) => (
        <button key={id} type="button" onClick={() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" })}
                className="border px-2.5 py-1 text-xs hover:border-foreground/40 hover:bg-accent/50">
          {label}{n != null && <span className="ml-1.5 text-muted-foreground tabular-nums">{n}</span>}
        </button>
      ))}
    </nav>
  )
}
