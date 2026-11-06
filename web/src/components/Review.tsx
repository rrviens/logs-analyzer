import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import { useAnalysis } from "@/lib/analysis"
import { Deaths } from "@/components/Deaths"
import { Findings } from "@/components/Findings"
import { DpsWindows, GoodCalls, Items, Pulls, Spells } from "@/components/Sections"
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
    <div className="flex flex-col gap-8">
      {(a.updating || (!result && a.progress.length > 0)) && (
        <p className="rounded-lg border bg-secondary px-3 py-2 text-sm text-muted-foreground">
          {a.updating || `Your run is ready; comparing with peers... ${a.progress.at(-1) || ""}`}
        </p>
      )}
      <section className="flex flex-col gap-5">
        <RunHeader res={shown} url={url} onReanalyze={saved ? undefined : a.reanalyze} busy={busy} />
        <Group res={shown} />
      </section>
      {shown.timeline?.pulls?.length ? <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}><Timeline ref={tl} res={shown} onPull={setLookPull} /></Suspense> : null}
      {result?.findings ? (
        <Findings findings={result.findings} level={result.run.level} model={result.compared?.model} best={best}
                  pull={lookPull} onShowAll={() => setLookPull(null)} player={result.run.player}
                  deathFor={(f) => (f.rule === "death" && result.deaths?.find((d) => d.clock === f.clock && f.title.includes(d.victim))) || null} />
      ) : (
        <p className="text-sm text-muted-foreground">Findings appear when the comparison with your peers is done.</p>
      )}
      {result?.deaths && <Deaths deaths={result.deaths} player={result.run.player} classes={Object.fromEntries(result.group.map((m) => [m.name, m.class]))}
                                   findingFor={(d) => result.findings?.find((f) => f.rule === "death" && f.clock === d.clock && f.title.includes(d.victim)) || null} />}
      {result && (
        <>
          <Items res={result} />
          <Spells res={result} />
          <DpsWindows res={result} />
          <Pulls res={result} />
          <GoodCalls res={result} />
          <p className="text-xs text-muted-foreground">
            {result.encounter?.covered ? "Ability notes are our own, written from log evidence across top runs."
              : "No ability notes for this dungeon yet: everything here is read from combat logs, not the encounter journal - if a mechanic has a side effect the log can't show (e.g. dispelling something is punished), the advice changes."}
            {" "}Defensive values in data/defensives.json are approximate.
          </p>
        </>
      )}
    </div>
    </TimelineContext.Provider>
  )
}
