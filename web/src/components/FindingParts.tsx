import { type Finding, type Line } from "@/lib/review"
import { Tx } from "@/lib/timeline"
import { cn } from "@/lib/utils"

// The rules' short labels, said as the question each answers. A death's "Ready?" lists the victim's own tools;
// on cooldown findings it is about that one cooldown.
const LABELS: Record<string, string> = { "Needed?": "Was it needed", "Group tools": "Other tools up", "Avoidable?": "Avoidable",
                                         "Interrupts": "Interrupts", "Situation": "Situation" }
const label = (k: string, rule: string) => k === "Ready?" ? (rule === "death" ? "What was ready" : "Was it ready") : LABELS[k] || k

// A finding opened: one reading order for every finding - what was ready, the evidence, how peers compare, side notes.
export function FindingParts({ f }: { f: Finding }) {
  const p = f.parts
  return (
    <>
      {p ? (
        <dl className="flex flex-col divide-y rounded-md border">
          {p.verdict.map(([k, v]) => <Part key={k} label={label(k, f.rule)}>{v.split("\n").map((l, i) => <p key={i}><Tx>{l}</Tx></p>)}</Part>)}
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
              {p.peer_context.flatMap(([k, v]) => v.split("\n").map((l, i) => <p key={`${k}${i}`}><Tx>{l}</Tx></p>))}
              <Lines items={p.peers} />
            </Part>
          )}
          {p.notes.length > 0 && (
            <Part label="Also" muted><Lines items={p.notes.map((n) => {
              const l = typeof n === "string" ? { text: n, sub: [] } : n  // results saved before rules 77 had plain strings
              return { ...l, text: l.text.replace(/^Also:\s*/, "") }
            })} /></Part>
          )}
        </dl>
      ) : null}
      {p ? null : (
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
    </>
  )
}

function Part({ label, muted, children }: { label: string; muted?: boolean; children: React.ReactNode }) {
  return (
    <div className="grid gap-x-6 gap-y-1 px-3 py-2.5 sm:grid-cols-[9.5rem_1fr]">
      <dt className="text-sm font-medium">{label}</dt>
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
