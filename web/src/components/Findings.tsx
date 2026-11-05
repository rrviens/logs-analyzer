import { useState } from "react"
import { ChevronRight, TriangleAlert } from "lucide-react"
import { byTime, type Finding, pullShort, reviewTier, SEV_LABEL, type Sev, sevOf, splitFindings, type Strict, strictPick } from "@/lib/review"
import { lsSet } from "@/lib/wow"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

const SEV_TONE: Record<Sev, string> = { high: "text-high bg-high/12", med: "text-med bg-med/12", low: "text-low bg-low/12" }
const SEV_EDGE: Record<Sev, string> = { high: "before:bg-high", med: "before:bg-med", low: "before:bg-low" }
const STRICT: [Strict, string][] = [["auto", "Auto"], ["relaxed", "Relaxed"], ["normal", "Normal"], ["strict", "Strict"]]

// "What to look at": High / Medium / Low, each in run order; what the review tier leaves out folds into minor notes.
export function Findings({ findings, level, model, best }: { findings: Finding[]; level: number; model?: string; best: number | null }) {
  const [pick, setPick] = useState<Strict>(strictPick)
  const tier = reviewTier(level, best, pick)
  const { shown, minor } = splitFindings(findings, level, model, tier)
  const choose = (p: Strict) => { lsSet("strict", p); setPick(p) }  // same setting as the classic page's Display menu

  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid size-8 place-items-center rounded-lg bg-high/12 text-high"><TriangleAlert className="size-4" /></span>
        <h2 className="text-lg font-semibold">What to look at</h2>
        <span className="rounded-full bg-secondary px-2 text-sm text-muted-foreground tabular-nums">{shown.length}</span>
        <div className="ml-auto flex rounded-lg border p-0.5 text-xs" role="group" aria-label="How strict the review is">
          {STRICT.map(([k, label]) => (
            <button key={k} onClick={() => choose(k)}
                    className={cn("rounded-md px-2.5 py-1 font-medium", pick === k ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground")}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <p className="text-sm text-muted-foreground">{tier.line}</p>
      {!shown.length && <p className="text-sm text-muted-foreground">Nothing worth flagging - this run holds up against your peers.</p>}
      {(["high", "med", "low"] as Sev[]).map((sev) => {
        const group = byTime(shown.filter((f) => sevOf(f.severity) === sev))
        return group.length > 0 && (
          <div key={sev} className="flex flex-col gap-1.5">
            <h3 className={cn("mt-2 text-xs font-semibold tracking-wider uppercase", SEV_TONE[sev].split(" ")[0])}>
              {SEV_LABEL[sev]} <span className="font-normal text-muted-foreground">{group.length}</span>
            </h3>
            {group.map((f, i) => <FindingCard key={`${f.rule}-${f.t_s}-${i}`} f={f} />)}
          </div>
        )
      })}
      {minor.length > 0 && (
        <Collapsible className="mt-2">
          <CollapsibleTrigger className="group flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
            <ChevronRight className="size-4 transition-transform group-data-[panel-open]:rotate-90" />
            {minor.length} minor note{minor.length > 1 ? "s" : ""} ({tier.minorWhy})
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2 flex flex-col gap-1.5">
            {byTime(minor).map((f, i) => <FindingCard key={`m-${f.rule}-${f.t_s}-${i}`} f={f} />)}
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  )
}

function FindingCard({ f }: { f: Finding }) {
  const sev = sevOf(f.severity)
  return (
    <Collapsible className={cn("relative overflow-hidden rounded-lg border bg-secondary before:absolute before:inset-y-0 before:left-0 before:w-1", SEV_EDGE[sev])}>
      <CollapsibleTrigger className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 py-2.5 pr-3 pl-4 text-left hover:bg-accent">
        <span className="w-10 text-sm text-muted-foreground tabular-nums">{f.pull !== "Whole run" ? f.clock : "run"}</span>
        <span className="text-xs text-muted-foreground">{pullShort(f.pull)}</span>
        <span className="min-w-48 flex-1 font-semibold">{f.short || f.title}</span>
        {f.fix && <span className="rounded-full bg-med/12 px-2 py-0.5 text-xs font-semibold whitespace-nowrap text-med">→ {f.fix}</span>}
        <span className={cn("rounded-full px-2 py-0.5 text-xs font-semibold", SEV_TONE[sev])} title={`Impact score ${f.severity}`}>{SEV_LABEL[sev]}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-2 pr-4 pb-3 pl-[4.25rem] text-sm">
        <p className="text-muted-foreground">{f.title} · {f.pull}</p>
        {f.context?.length > 0 && (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
            {f.context.map(([k, v]) => (
              <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></div>
            ))}
          </dl>
        )}
        {f.detail.length > 0 && <ul className="list-disc space-y-0.5 pl-4">{f.detail.map((d, i) => <li key={i}>{d}</li>)}</ul>}
      </CollapsibleContent>
    </Collapsible>
  )
}
