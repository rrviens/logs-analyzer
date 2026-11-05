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
import { TimelineContext } from "@/lib/timeline"

// The chart library is big: the timeline loads after the rest of the review.
const Timeline = lazy(() => import("@/components/Timeline").then((m) => ({ default: m.Timeline })))

// One key's review: header and group, timeline, then the sections.
export function Review({ url, onReady }: { url: string; onReady?: () => void }) {
  const a = useAnalysis(url)
  const shown = a.result || a.preview
  useEffect(() => { if (shown) onReady?.() }, [!!shown])  // eslint-disable-line react-hooks/exhaustive-deps
  const tl = useRef<TimelineHandle>(null)
  const ctx = useMemo(() => ({ focus: (s: number, e?: number | null) => tl.current?.focus(s, e) }), [])
  const [best, setBest] = useState<number | null>(null)  // your best timed key (Raider.io): sets how strict the review is
  useEffect(() => {
    api<{ best: number | null }>(`/api/best?url=${encodeURIComponent(url)}`).then((d) => setBest(d.best ?? null)).catch(() => {})
  }, [url])
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
  const busy = !a.result || !!a.updating
  return (
    <TimelineContext.Provider value={ctx}>
    <div className="flex flex-col gap-5">
      {(a.updating || (!a.result && a.progress.length > 0)) && (
        <p className="rounded-lg border bg-secondary px-3 py-2 text-sm text-muted-foreground">
          {a.updating || `Your run is ready; comparing with peers... ${a.progress.at(-1) || ""}`}
        </p>
      )}
      <section className="flex flex-col gap-5 rounded-xl border bg-card p-5">
        <RunHeader res={shown} url={url} onReanalyze={a.reanalyze} busy={busy} />
        <Group res={shown} />
      </section>
      {shown.timeline?.pulls?.length ? <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}><Timeline ref={tl} res={shown} /></Suspense> : null}
      {a.result?.findings ? (
        <Findings findings={a.result.findings} level={a.result.run.level} model={a.result.compared?.model} best={best} />
      ) : (
        <p className="text-sm text-muted-foreground">Findings appear when the comparison with your peers is done.</p>
      )}
      {a.result?.deaths && <Deaths deaths={a.result.deaths} player={a.result.run.player} />}
      {a.result && (
        <>
          <Items res={a.result} />
          <Spells res={a.result} />
          <DpsWindows res={a.result} />
          <Pulls res={a.result} />
          <GoodCalls res={a.result} />
        </>
      )}
    </div>
    </TimelineContext.Provider>
  )
}
