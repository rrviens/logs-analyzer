import { ChevronRight } from "lucide-react"
import type { Analysis } from "@/lib/analysis"
import { pullShort } from "@/lib/review"
import { Tx, useFocus } from "@/lib/timeline"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

// The verdict sections after the death recaps, ported from the classic page: a headline, the lines behind it, then the data.
export type Verdict = { headline: string; ok: boolean; lines: { text: string; action: boolean; examples: string[] }[] }

const pct = (v?: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`)
const k = (v?: number | null) => (v == null ? "-" : `${Math.round(v / 1000)}k`)
const dur = (s: number) => (s >= 90 ? `${Math.floor(s / 60)} min ${s % 60}s` : `${s}s`)
const m = (v: number) => `${(v / 1e6).toFixed(1)}M`

function Section({ title, count, empty, children }: { title: string; count: number | string; empty: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4 border-t pt-7">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        <span className="text-sm text-muted-foreground tabular-nums">{count || 0}</span>
      </div>
      {count ? children : <p className="text-sm text-muted-foreground">{empty}</p>}
    </section>
  )
}

const todo = (v?: Verdict | null) => v?.lines?.filter((l) => l.action).length || 0
const countOf = (has: boolean, v?: Verdict | null) => (has ? todo(v) || "ok" : 0)

function Fold({ label, className, children }: { label: React.ReactNode; className?: string; children: React.ReactNode }) {
  return (
    <Collapsible className={cn("border-y", className)}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-1 py-2.5 text-left text-sm hover:bg-accent/50">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[panel-open]:rotate-90" />{label}
      </CollapsibleTrigger>
      <CollapsibleContent className="mb-4 ml-3 flex flex-col gap-4 border-l pl-5 text-sm">{children}</CollapsibleContent>
    </Collapsible>
  )
}

// Headline collapsed; the lines (→ to do, ✓ fine) and the raw data on expand.
function VerdictBlock({ v, children }: { v?: Verdict | null; children?: React.ReactNode }) {
  if (!v?.headline) return <>{children}</>
  return (
    <Fold label={<span className={cn("font-semibold", v.ok ? "text-good" : "text-med")}>{v.ok ? "✓" : "→"} {v.headline}</span>}>
      {v.lines.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {v.lines.map((l, i) => (
            <li key={i} className="flex gap-2">
              <span className={cn("font-bold", l.action ? "text-med" : "text-good")}>{l.action ? "→" : "✓"}</span>
              <span><Tx>{l.text}</Tx>
                {l.examples.length > 0 && (
                  <span className="mt-0.5 flex flex-wrap gap-1.5">
                    {l.examples.map((e, j) => <span key={j} className="text-xs text-muted-foreground"><Tx>{e}</Tx></span>)}
                  </span>
                )}</span>
            </li>
          ))}
        </ul>
      )}
      {children}
    </Fold>
  )
}

function Table({ head, rows, note }: { head: React.ReactNode[]; rows: React.ReactNode[][]; note?: React.ReactNode }) {
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b">{head.map((h, i) => <th key={i} className="py-1.5 pr-4 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody className="tabular-nums">
            {rows.map((r, i) => <tr key={i} className="border-b border-border/50 last:border-0">{r.map((c, j) => <td key={j} className="py-1.5 pr-4">{c}</td>)}</tr>)}
          </tbody>
        </table>
      </div>
      {note && <p className="mt-1.5 text-xs text-muted-foreground">{note}</p>}
    </div>
  )
}

// Each trinket / consumable as one compact block (fits the side column): what to do with it, your uses against peer
// healers' as two bars, how long it sat ready under pressure, and how often it went out with your big cooldowns.
export function Items({ res }: { res: Analysis }) {
  const items = res.items || [], v = res.verdicts?.items
  const linesOf = (name: string) => (v?.lines || []).filter((l) => l.text.includes(name))
  return (
    <Section title="Trinkets & consumables" count={countOf(items.length > 0, v)} empty="Trinkets & consumables: none used.">
      <Fold label={v?.headline
        ? <span className={cn("font-semibold", v.ok ? "text-good" : "text-med")}>{v.ok ? "✓" : "→"} {v.headline}</span>
        : <span className="font-semibold">{items.length} used</span>}>
        <div className="flex flex-col">
          {items.map((i) => {
            const lines = linesOf(i.name)
            // → what the review asks you to change; • a softer suggestion (potions); ✓ fine as it is
            const glyph = lines.some((l) => l.action) ? ["→", "text-med"] : lines.some((l) => /^(Press|Line) /.test(l.text))
              ? ["•", "text-muted-foreground"] : ["✓", "text-good"]
            const top = Math.max(i.uses, i.top_uses || 0, 1)
            return (
              <div key={i.name} className="flex flex-col gap-2 border-b py-3 first:pt-0 last:border-0 last:pb-0">
                <div className="flex items-baseline gap-2">
                  <span className={cn("font-semibold", glyph[1])} aria-hidden>{glyph[0]}</span>
                  <span className="font-medium"><Tx>{i.name}</Tx></span>
                  <span className="ml-auto text-xs text-muted-foreground">{i.kind}</span>
                </div>
                {lines.map((l, k) => (
                  <p key={k} className="text-muted-foreground"><Tx>{l.text.replace(`${i.name}: `, "").replace(/ \(\d+ of \d+ peer healers use it\)\.?$/, ".")}</Tx></p>
                ))}
                <div className="grid grid-cols-[3.5rem_1fr_2.5rem] items-center gap-x-2 gap-y-1 text-xs">
                  <span className="text-muted-foreground">You</span>
                  <span className="h-1.5 rounded-full bg-muted"><span className="block h-full rounded-full bg-data" style={{ width: `${(i.uses / top) * 100}%` }} /></span>
                  <span className="text-right tabular-nums">{i.uses}</span>
                  <span className="text-muted-foreground">Peers</span>
                  <span className="h-1.5 rounded-full bg-muted"><span className="block h-full rounded-full bg-muted-foreground/50" style={{ width: `${((i.top_uses || 0) / top) * 100}%` }} /></span>
                  <span className="text-right text-muted-foreground tabular-nums">{i.top_uses == null ? "-" : `~${Math.round(i.top_uses)}`}</span>
                </div>
                <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
                  <dt className="text-muted-foreground">Used by</dt><dd>{i.top_users} of {i.top_count} peer healers</dd>
                  {i.ready_pressure_s != null && <><dt className="text-muted-foreground">Sat ready under pressure</dt><dd>{dur(i.ready_pressure_s)}</dd></>}
                  {i.paired != null && (
                    <><dt className="text-muted-foreground">With your big cooldowns</dt>
                      <dd>{pct(i.paired)}<span className="text-muted-foreground"> (peers {pct(i.top_paired)})</span></dd></>
                  )}
                </dl>
                {!!i.ready_examples?.length && (
                  <Collapsible>
                    <CollapsibleTrigger className="group flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                      <ChevronRight className="size-3 transition-transform group-data-[panel-open]:rotate-90" />When it sat ready
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-1 flex flex-col gap-0.5 border-l pl-3 text-xs text-muted-foreground">
                      {i.ready_examples.map((e, k) => <p key={k}><Tx>{e}</Tx></p>)}
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </div>
            )
          })}
        </div>
        <p className="text-xs text-muted-foreground">Peer uses: median among the peer healers who used it, scaled to your run length. Big cooldowns:
          within 10s of {res.spec.majors || "your major cooldowns"}.</p>
      </Fold>
    </Section>
  )
}

const SITUATION = { none: "Nobody injured", one: "1 player injured", two: "2 players injured", group: "3+ players injured" }

export function Spells({ res }: { res: Analysis }) {
  const sc = res.spell_choice, G = res.spec.group || "Group heal", S = res.spec.single || "Single-target heal"
  return (
    <Section title={`${S} vs ${G}`} count={countOf(!!sc?.you, res.verdicts?.spells)}
             empty="Not enough heals to judge.">
      {sc?.you && (
        <VerdictBlock v={res.verdicts?.spells}>
          <Table head={["Situation at cast", "Your heals", `Your ${G} share`, `Top ${G} share`, `${G} overheal (you / top)`,
                        `${S} overheal (you / top)`, `Your healing per cast (${G} / ${S})`, "Your mana"]}
                 rows={(Object.keys(SITUATION) as (keyof typeof SITUATION)[]).map((s) => {
                   const y = sc.you![s], t: Partial<typeof y> = sc.top?.[s] || {}
                   return [SITUATION[s], y.casts, pct(y.ch_share), pct(t.ch_share), `${pct(y.ch_overheal)} / ${pct(t.ch_overheal)}`,
                           `${pct(y.hw_overheal)} / ${pct(t.hw_overheal)}`, `${k(y.ch_effective)} / ${k(y.hw_effective)}`, pct(y.mana)]
                 })}
                 note={`"Injured" = below 80% HP just before the heal landed. Healing per cast counts every target hit.`} />
          {Object.values(sc.buffs?.you || {}).some((y) => y.casts) && (
            <Table head={["Buff up at cast", "Your heals", `Your ${G} share`, `Top ${G} share`, `Your per cast (${G} / ${S})`, `Top per cast (${G} / ${S})`]}
                   rows={Object.entries(sc.buffs!.you).filter(([, y]) => y.casts).map(([name, y]) => {
                     const t: Partial<typeof y> = sc.buffs!.top?.[name] || {}
                     return [name, y.casts, pct(y.ch_share), pct(t.ch_share), `${k(y.ch_effective)} / ${k(y.hw_effective)}`,
                             `${k(t.ch_effective)} / ${k(t.hw_effective)}`]
                   })} />
          )}
        </VerdictBlock>
      )}
    </Section>
  )
}

const useTone = (u?: number | null) => (u == null ? "bg-muted-foreground" : u >= 0.75 ? "bg-good" : u >= 0.4 ? "bg-warn" : "bg-bad")

export function DpsWindows({ res }: { res: Analysis }) {
  const d = res.dps, ws = res.timeline?.dps_windows || [], pulls = res.timeline?.pulls || []
  const focus = useFocus()
  const worst = ws.filter((w) => w.missed >= 5e4).sort((a, b) => b.missed - a.missed).slice(0, 6)
  return (
    <Section title="DPS windows" count={ws.length ? ws.filter((w) => w.use != null && w.use < 0.4).length || "ok" : 0}
             empty="No calm stretches long enough to deal damage in this run.">
      {d && (
        <p className="text-sm">
          {d.windows} calm window{d.windows === 1 ? "" : "s"} ({d.seconds}s in total){d.your_dps ? `: you dealt ${k(d.your_dps)}/s in them` : ""}
          {d.top_dps ? `, peer ${res.spec.plural} ${k(d.top_dps)}/s in their own calm moments` : ""}
          {d.missed ? <span className="text-muted-foreground"> · ~{m(d.missed)} damage left on the table (your own hit sizes × enemies alive)</span> : null}
        </p>
      )}
      {/* Pulls side by side (gaps between pulls left out), each window placed inside its pull. */}
      <div className="flex gap-1.5">
        {pulls.map((p, i) => (
          <div key={i} style={{ flexGrow: p.end_s - p.start_s }} className="min-w-0 basis-0">
            <div className={cn("truncate text-[11px] text-muted-foreground", p.boss && "font-semibold text-foreground")}>{pullShort(p.label)}</div>
            <div className={cn("relative h-6 rounded", p.boss ? "bg-primary/12" : "bg-muted")}>
              {ws.filter((w) => w.start_s >= p.start_s && w.start_s <= p.end_s).map((w, j) => (
                <button key={j} type="button" onClick={() => focus(w.start_s, w.end_s)} title={[`${w.clock} · ${pullShort(w.pull)}`, ...w.tip.filter(Boolean)].join("\n")}
                      className={cn("absolute inset-y-1 min-w-1 rounded-sm opacity-80 hover:opacity-100", useTone(w.use))}
                      style={{ left: `${(w.start_s - p.start_s) / (p.end_s - p.start_s) * 100}%`,
                               width: `${(Math.min(w.end_s, p.end_s) - w.start_s) / (p.end_s - p.start_s) * 100}%` }} />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {([["bg-good", "used like your peers"], ["bg-warn", "partly used"], ["bg-bad", "mostly missed"]] as const).map(([c, l]) => (
          <span key={l} className="flex items-center gap-1.5"><i className={cn("size-2.5 rounded-sm", c)} />{l}</span>
        ))}
        <span>A window = 3s+ with the group healthy, nobody low and no big hit coming. Hover one for details, click to open it on the timeline.</span>
      </div>
      {worst.length > 0 && (
        <>
          <h3 className="mt-1 text-sm font-medium">Biggest missed windows</h3>
          <ul className="flex flex-col text-sm">
            {worst.map((w, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-3 border-b px-1 py-2 first:border-t">
                <span className="w-10 tabular-nums"><Tx>{w.clock}</Tx></span>
                <span className="text-xs text-muted-foreground">{pullShort(w.pull)}</span>
                <span className="flex-1">{w.seconds}s calm, {w.dps_casts} damage cast{w.dps_casts === 1 ? "" : "s"}, {w.other_casts} other, ~{w.idle_s}s idle</span>
                <b className="text-bad">~{m(w.missed)} missed</b>
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  )
}

export function Pulls({ res }: { res: Analysis }) {
  const reqs = res.requirements || []
  const rows: React.ReactNode[][] = []
  for (const q of reqs) {
    rows.push([q.pull, `${Math.round(q.duration_s)}s`,
               <span className={cn(q.top_group_hp != null && q.group_hp < q.top_group_hp - 0.03 && "font-semibold text-bad")}>{pct(q.group_hp)}</span>,
               pct(q.top_group_hp), k(q.intake), k(q.top_intake), k(q.healing), k(q.top_healing)])
    // Boss fights get a row per part (named after what began it).
    for (const ph of (res.phases || []).filter((ph) => ph.pull.split(":")[0] === q.pull.split(":")[0])) {
      rows.push([<span className="pl-4 text-muted-foreground">{ph.phase === "Start" ? "start" : `after ${ph.phase}`} · {ph.clock}</span>, `${ph.duration_s}s`,
                 <span className={cn(ph.top_group_hp != null && ph.group_hp < ph.top_group_hp - 0.05 && "font-semibold text-bad")}>{pct(ph.group_hp)}</span>,
                 pct(ph.top_group_hp),
                 <span className="text-muted-foreground">damage taken {(ph.intake * 100).toFixed(1)}%/s{ph.top_intake == null ? "" : ` vs ${(ph.top_intake * 100).toFixed(1)}%/s`}</span>,
                 "", "", ""])
    }
  }
  return (
    <Section title="Pulls vs peers" count={countOf(reqs.length > 0, res.verdicts?.pulls)} empty="No pulls to compare.">
      <VerdictBlock v={res.verdicts?.pulls}>
        <Table head={["Pull", "Time", "Group HP", "Top HP", "Dmg in/s", "Top in/s", "Heal/s", "Top heal/s"]} rows={rows} />
      </VerdictBlock>
    </Section>
  )
}

export function GoodCalls({ res }: { res: Analysis }) {
  const good = res.good_calls || []
  return (
    <Section title="Good calls" count={good.length} empty="Good calls: none recorded.">
      <Fold label={<span className="font-semibold text-good">✓ {good.length} cooldowns pressed at the right moment</span>}>
        {good.map((g, i) => (
          <p key={i}><span className="text-good">✓</span> <Tx>{g.title}</Tx> <span className="text-xs text-muted-foreground">{pullShort(g.pull)}</span>
            <span className="block pl-4 text-muted-foreground"><Tx>{g.detail.join(" ")}</Tx></span></p>
        ))}
      </Fold>
    </Section>
  )
}
