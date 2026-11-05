import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { LogRow } from "@/components/LogRow"
import { byTime, type Finding, pullShort, reviewTier, SEV_LABEL, type Sev, sevOf, splitFindings, type Strict, strictPick } from "@/lib/review"
import { lsSet } from "@/lib/wow"
import { Tx } from "@/lib/timeline"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

const SEV_MARK: Record<Sev, string> = { high: "bg-high", med: "bg-med", low: "bg-low" }
const SEV_HEAD: Record<Sev, string> = { high: "High impact", med: "Medium impact", low: "Low impact" }
const STRICT: [Strict, string][] = [["auto", "Auto"], ["relaxed", "Relaxed"], ["normal", "Normal"], ["strict", "Strict"]]

// "What to look at": High / Medium / Low, each in run order; what the review tier leaves out folds into minor notes.
export function Findings({ findings, level, model, best, pull, onShowAll }: {
  findings: Finding[]; level: number; model?: string; best: number | null
  pull?: { label: string; start_s: number; end_s: number } | null; onShowAll?: () => void
}) {
  const [pick, setPick] = useState<Strict>(strictPick)
  const tier = reviewTier(level, best, pick)
  const split = splitFindings(findings, level, model, tier)
  // A pull picked on the timeline narrows the list to it (whole-run notes drop out).
  const inPull = (f: Finding) => !pull || (f.pull !== "Whole run" && f.t_s >= pull.start_s - 1 && f.t_s <= pull.end_s + 1)
  const shown = split.shown.filter(inPull), minor = split.minor.filter(inPull)
  const choose = (p: Strict) => { lsSet("strict", p); setPick(p) }  // same setting as the classic page's Display menu

  return (
    <section className="flex flex-col gap-4 border-t pt-7">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold tracking-tight">What to look at</h2>
        <span className="text-sm text-muted-foreground tabular-nums">{shown.length}</span>
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
      {pull && (
        <div className="flex flex-wrap items-center gap-3 border-l-2 border-foreground py-1 pl-3 text-sm">
          <span>{shown.length ? `Showing ${shown.length} of ${split.shown.length} for` : "Nothing flagged in"} <b>{pull.label}</b></span>
          <button className="ml-auto underline underline-offset-2 hover:no-underline" onClick={onShowAll}>Show the whole dungeon</button>
        </div>
      )}
      {!shown.length && !pull && <p className="text-sm text-muted-foreground">Nothing worth flagging - this run holds up against your peers.</p>}
      {(["high", "med", "low"] as Sev[]).map((sev) => {
        const group = byTime(shown.filter((f) => sevOf(f.severity) === sev))
        return group.length > 0 && (
          <div key={sev} className="flex flex-col">
            <h3 className="mt-3 mb-2 flex items-baseline gap-2 text-sm font-medium">
              {SEV_HEAD[sev]} <span className="font-normal text-muted-foreground tabular-nums">{group.length}</span>
            </h3>
            {group.map((f, i) => <FindingCard key={`${f.rule}-${f.t_s}-${i}`} f={f} id={findings.indexOf(f)} />)}
          </div>
        )
      })}
      {minor.length > 0 && (
        <Collapsible className="mt-2">
          <CollapsibleTrigger className="group flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
            <ChevronRight className="size-4 transition-transform group-data-[panel-open]:rotate-90" />
            {minor.length} minor note{minor.length > 1 ? "s" : ""} ({tier.minorWhy})
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2 flex flex-col">
            {byTime(minor).map((f, i) => <FindingCard key={`m-${f.rule}-${f.t_s}-${i}`} f={f} id={findings.indexOf(f)} />)}
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  )
}

function FindingCard({ f, id }: { f: Finding; id: number }) {
  const sev = sevOf(f.severity)
  return (
    <LogRow id={`finding-${id}`} time={f.pull !== "Whole run" ? f.clock : "run"} mark={SEV_MARK[sev]} fix={f.fix}
            title={<><span className="font-medium">{f.short || f.title}</span>
              <span className="ml-2 text-xs text-muted-foreground" title={`${SEV_LABEL[sev]} impact, score ${f.severity}`}>{pullShort(f.pull)}</span></>}>
      <p className="text-muted-foreground">{f.title}, {f.pull}</p>
      {f.context?.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
          {f.context.map(([k, v]) => (
            <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd><Tx>{v}</Tx></dd></div>
          ))}
        </dl>
      )}
      {f.detail.length > 0 && <ul className="list-disc space-y-0.5 pl-4 marker:text-muted-foreground">{f.detail.map((d, i) => <li key={i}><Tx>{d}</Tx></li>)}</ul>}
    </LogRow>
  )
}
