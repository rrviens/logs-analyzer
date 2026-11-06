import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { LogRow } from "@/components/LogRow"
import { byTime, type Finding, type Line, pullShort, reviewTier, SEV_LABEL, type Sev, sevOf, splitFindings, type Strict, strictPick } from "@/lib/review"
import { lsSet } from "@/lib/wow"
import { reveal, Tx } from "@/lib/timeline"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

const SEV_MARK: Record<Sev, string> = { high: "bg-high", med: "bg-med", low: "bg-low" }
const SEV_HEAD: Record<Sev, string> = { high: "High impact", med: "Medium impact", low: "Low impact" }
const STRICT: [Strict, string][] = [["auto", "Auto"], ["relaxed", "Relaxed"], ["normal", "Normal"], ["strict", "Strict"]]

// "What to look at": High / Medium / Low, each in run order; what the review tier leaves out folds into minor notes.
export function Findings({ findings, level, model, best, pull, onShowAll, deathRecap }: {
  findings: Finding[]; level: number; model?: string; best: number | null
  pull?: { label: string; start_s: number; end_s: number } | null; onShowAll?: () => void
  deathRecap?: (f: Finding) => string | null  // the death recap's id for a death finding, to link instead of repeating it
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
            {group.map((f, i) => <FindingCard key={`${f.rule}-${f.t_s}-${i}`} f={f} id={findings.indexOf(f)} recap={deathRecap?.(f)} />)}
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
            {byTime(minor).map((f, i) => <FindingCard key={`m-${f.rule}-${f.t_s}-${i}`} f={f} id={findings.indexOf(f)} recap={deathRecap?.(f)} />)}
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  )
}

function FindingCard({ f, id, recap }: { f: Finding; id: number; recap?: string | null }) {
  const sev = sevOf(f.severity), p = f.parts
  return (
    <LogRow id={`finding-${id}`} time={f.pull !== "Whole run" ? f.clock : "run"} mark={SEV_MARK[sev]} fix={f.fix}
            title={<><span className="font-medium">{f.short || f.title}</span>
              <span className="ml-2 text-xs text-muted-foreground" title={`${SEV_LABEL[sev]} impact, score ${f.severity}`}>{pullShort(f.pull)}</span></>}>
      {p ? (
        // One reading order for every finding: what was ready, the evidence, how peers compare, side notes.
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-[9rem_1fr]">
          {p.verdict.map(([k, v]) => <Part key={k} label={k.replace(/\?$/, "")}><p><Tx>{v}</Tx></p></Part>)}
          {p.shares && (
            <Part label={`Damage, last ${p.shares.seconds}s`}>
              <div className="grid max-w-xl grid-cols-[minmax(0,auto)_1fr_2.5rem] items-center gap-x-3 gap-y-1">
                {p.shares.rows.map(([name, pct]) => (
                  <div key={name} className="contents">
                    <span className="truncate"><Tx>{name}</Tx></span>
                    <span className="h-1.5 rounded-full bg-muted"><span className="block h-full rounded-full bg-foreground/60" style={{ width: `${pct}%` }} /></span>
                    <span className="text-right text-muted-foreground tabular-nums">{pct}%</span>
                  </div>
                ))}
              </div>
            </Part>
          )}
          {p.why.length > 0 && <Part label="What happened"><Lines items={p.why} /></Part>}
          {p.rates.length > 0 && (
            <Part label="Damage vs peers">
              {p.rates_title && <p className="mb-1.5 text-muted-foreground">{p.rates_title}</p>}
              <RateRows rows={p.rates} />
            </Part>
          )}
          {(p.peers.length > 0 || p.peer_context.length > 0) && (
            <Part label="Compared with peers">
              {p.peer_context.map(([k, v]) => <p key={k}><Tx>{v}</Tx></p>)}
              <Lines items={p.peers} />
            </Part>
          )}
          {p.notes.length > 0 && (
            <Part label="Also" muted>{p.notes.map((n, i) => <p key={i}><Tx>{n.replace(/^Also:\s*/, "")}</Tx></p>)}</Part>
          )}
          {recap && (
            <Part label="">
              <button className="underline underline-offset-2 hover:no-underline" onClick={() => reveal(recap)}>Open the death recap</button>
            </Part>
          )}
        </dl>
      ) : (
        <>
          {f.context?.length > 0 && (
            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
              {f.context.map(([k, v]) => (
                <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd><Tx>{v}</Tx></dd></div>
              ))}
            </dl>
          )}
          {f.detail.length > 0 && <ul className="list-disc space-y-0.5 pl-4 marker:text-muted-foreground">{f.detail.map((d, i) => <li key={i}><Tx>{d}</Tx></li>)}</ul>}
        </>
      )}
    </LogRow>
  )
}

function Part({ label, muted, children }: { label: string; muted?: boolean; children: React.ReactNode }) {
  return (
    <div className="contents">
      <dt className="pt-px text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className={cn("flex min-w-0 flex-col gap-1", muted && "text-muted-foreground")}>{children}</dd>
    </div>
  )
}

function Lines({ items }: { items: Line[] }) {
  return (
    <ul className="flex flex-col gap-1">
      {items.map((l, i) => (
        <li key={i}>
          <Tx>{l.text}</Tx>
          {l.sub.length > 0 && (
            <ul className="mt-1 flex flex-col gap-0.5 border-l pl-3 text-muted-foreground">{l.sub.map((x, j) => <li key={j}><Tx>{x}</Tx></li>)}</ul>
          )}
        </li>
      ))}
    </ul>
  )
}

// Your group's damage taken per ability (share of group HP per second) against peer groups': two thin bars each.
function RateRows({ rows }: { rows: [string, number, number, string][] }) {
  const top = Math.max(...rows.flatMap(([, a, b]) => [a, b]), 0.1)
  return (
    <div className="grid max-w-xl grid-cols-[minmax(0,auto)_1fr_auto] items-center gap-x-3 gap-y-2">
      {rows.map(([name, mine, peer, note]) => (
        <div key={name} className="contents">
          <span className="truncate"><Tx>{name}</Tx></span>
          <span className="flex flex-col gap-0.5" title={`You ${mine}% of group HP per second, peers ${peer}%`}>
            <span className="h-1.5 rounded-full bg-data" style={{ width: `${(mine / top) * 100}%` }} />
            <span className="h-1.5 rounded-full bg-muted-foreground/40" style={{ width: `${(peer / top) * 100}%` }} />
          </span>
          <span className="text-xs text-muted-foreground tabular-nums">{mine}% vs {peer}%/s</span>
          {note && <span className="col-span-3 -mt-1 text-xs text-muted-foreground">{note}</span>}
        </div>
      ))}
      <span className="col-span-3 flex gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><i className="h-1.5 w-3 rounded-full bg-data" />your group</span>
        <span className="flex items-center gap-1.5"><i className="h-1.5 w-3 rounded-full bg-muted-foreground/40" />peer groups</span>
      </span>
    </div>
  )
}
