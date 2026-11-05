import { useState } from "react"
import { Skull } from "lucide-react"
import { pullShort } from "@/lib/review"
import { Tx } from "@/lib/timeline"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

type ReadyItem = { name: string; owner: string; status: string; text: string; verdict?: "lived" | "close" | "died"; lowest?: number;
                   escape?: boolean; no_window?: boolean; gcd?: boolean }
type Event = { dt: number; kind: "hit" | "heal" | "defensive"; ability: string; source?: string; school?: string; amount?: number;
               overkill?: number; overheal?: number; hp?: number | null }
export type Death = {
  clock: string; victim: string; pull: string; killing: string; short?: string; fix?: string; note?: string
  cause?: { name: string; lines: string[] }
  story?: { lines: string[]; advice: string[] } | null
  avoid?: { label: string; line: string } | null
  stoppable?: { ability: string; share: number; rate: number; kickable?: boolean; ready: { player: string; you: boolean; tools: string[] }[] } | null
  dodge?: { ability: string; why: string } | null
  skip?: string; pull_melee?: string
  after_tank?: { clock: string; victim: string }[]
  last_casts?: { dt: number; ability: string; target: string; hp: number | null }[]
  ready: { group: ReadyItem[]; victim: ReadyItem[]; healer: ReadyItem[] }
  taken: number; healed: number; max_hp: number | null; events: Event[]
}

const VERDICT = { lived: "Likely lived", close: "Too close to call", died: "Would still die" }
const VERDICT_TONE = { lived: "text-good bg-good/12", close: "text-med bg-med/12", died: "text-bad bg-bad/12" }
const k = (v?: number | null) => (v == null ? "" : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : `${Math.round(v / 1000)}k`)
export const deathId = (clock: string, victim: string) => `death-${clock}-${victim}`

const Pill = ({ className, title, children }: { className?: string; title?: string; children: React.ReactNode }) =>
  <span title={title} className={cn("rounded-full bg-muted px-2 py-0.5 text-xs font-semibold whitespace-nowrap", className)}>{children}</span>

// Each death: why it happened, what could have saved it (and from whom), and the last seconds of hits and heals.
export function Deaths({ deaths, player }: { deaths: Death[]; player: string }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <div className="flex items-center gap-3">
        <span className="grid size-8 place-items-center rounded-lg bg-bad/12 text-bad"><Skull className="size-4" /></span>
        <h2 className="text-lg font-semibold">Death recaps</h2>
        <span className="rounded-full bg-secondary px-2 text-sm text-muted-foreground tabular-nums">{deaths.length}</span>
      </div>
      {!deaths.length && <p className="text-sm text-muted-foreground">Deaths: none.</p>}
      {deaths.map((d) => {
        const id = deathId(d.clock, d.victim)
        return (
          <Collapsible key={id} id={id}
                       className="scroll-mt-4 overflow-hidden rounded-lg border bg-secondary">
            <CollapsibleTrigger className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left hover:bg-accent">
              <span className="w-10 text-sm text-muted-foreground tabular-nums">{d.clock}</span>
              <span className="text-xs text-muted-foreground">{pullShort(d.pull)}</span>
              <span className="min-w-48 flex-1"><b>{d.victim}</b> {d.short || `died to ${d.killing}`}</span>
              {d.avoid && <Pill className="bg-med/12 text-med" title={d.avoid.line}>{d.avoid.label}</Pill>}
              {d.stoppable && <Pill className="bg-high/12 text-high" title="A cast peer groups usually stop did most of the damage">Stoppable</Pill>}
              {d.fix && <Pill className="bg-med/12 text-med">→ {d.fix}</Pill>}
            </CollapsibleTrigger>
            <CollapsibleContent className="flex flex-col gap-2.5 px-4 pb-4 text-sm">
              <DeathBody d={d} player={player} />
            </CollapsibleContent>
          </Collapsible>
        )
      })}
    </section>
  )
}

function Story({ label, children, tone }: { label: string; children: React.ReactNode; tone?: string }) {
  return <p><b className={tone}>{label}</b> {children}</p>
}

function DeathBody({ d, player }: { d: Death; player: string }) {
  const [all, setAll] = useState(false)
  const events = all ? d.events : d.events.slice(-8)
  const s = d.stoppable
  return (
    <>
      {d.story?.advice.length ? (
        <div className="rounded-md border-l-3 border-med bg-med/10 px-3 py-2">
          <b>What to do:</b>
          <ul className="mt-1 list-disc pl-5">{d.story.advice.map((a, i) => <li key={i}><Tx>{a}</Tx></li>)}</ul>
        </div>
      ) : null}
      {d.note && <Story label={`About ${d.cause?.name || d.killing}:`}>{d.note}</Story>}
      {d.avoid && <Story label={`${d.avoid.label}:`} tone="text-med">{d.avoid.line.split(" - ").slice(1).join(" - ")}</Story>}
      {d.skip && <Story label="Skip went wrong:">died alone with nobody else in combat while the group was skipping ({d.skip}) - not a healing problem.</Story>}
      {d.pull_melee && <Story label="Threat, not healing:">mob melee {d.pull_melee} - a DPS the pack goes for first is one-shot before any heal lands.</Story>}
      {d.dodge && d.dodge.why !== d.note && <Story label="Dodge it:">{d.dodge.why} - no defensive is the answer.</Story>}
      {!!d.after_tank?.length && (
        <Story label="Wipe:">after the tank died - {d.after_tank.map((a) => `${a.victim} (${a.clock})`).join(", ")}. The pull was lost from
          here, so these deaths aren't analyzed.</Story>
      )}
      {s && (
        <Story label="Could likely have been stopped:" tone="text-high">
          {s.ability} did {Math.round(s.share * 100)}% of the damage, and peer groups stop that cast {Math.round(s.rate * 100)}% of the time
          ({s.kickable === false ? "it can't be interrupted - stun or CC only" : "interrupt, stun or CC"}). Ready then:{" "}
          {s.ready.length ? s.ready.map((x) => `${x.you ? "you" : x.player} - ${x.tools.join(", ")}`).join("; ") : "nothing - every interrupt and stun was on cooldown"}.
        </Story>
      )}
      {d.cause && (
        <Story label="Cause:"><Tx>{d.cause.lines.join(" · ")}</Tx>{d.cause.name !== d.killing ? ` · killing blow ${d.killing}` : ""}</Story>
      )}
      {d.story && <ul className="list-disc pl-5 text-muted-foreground">{d.story.lines.map((l, i) => <li key={i}><Tx>{l}</Tx></li>)}</ul>}
      {!!d.last_casts?.filter((x) => x.target).length && (
        <Story label="Last casts:">
          {d.last_casts.filter((x) => x.target).map((x) => `${x.ability} → ${x.target}${x.hp != null ? ` @${Math.round(x.hp * 100)}%` : ""} (${x.dt.toFixed(1)}s)`).join(" · ")}
        </Story>
      )}
      <div className="grid gap-3 md:grid-cols-3">
        <ReadyCol title="Group" items={d.ready.group} owner />
        <ReadyCol title={d.victim === player ? `${d.victim} (you, died)` : `${d.victim} (died)`} items={d.ready.victim} />
        <ReadyCol title={`${player} (healer)`} items={d.ready.healer} />
      </div>
      <p className="text-xs text-muted-foreground">Last 10s: took {k(d.taken)}, healed {k(d.healed)} (max HP {k(d.max_hp)}).</p>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-muted-foreground"><tr className="text-left">
            <th className="py-1 pr-3 font-medium">Before death</th><th className="pr-3" /><th className="pr-3 font-medium">Ability</th>
            <th className="pr-3 font-medium">From</th><th className="pr-3 font-medium">Amount</th><th className="font-medium">HP after</th>
          </tr></thead>
          <tbody>
            {events.map((e, i) => (
              <tr key={i} className="border-t border-border/60">
                <td className="py-1 pr-3 tabular-nums">{e.dt.toFixed(1)}s</td>
                <td className={cn("pr-3 font-semibold", e.kind === "hit" ? "text-bad" : e.kind === "heal" ? "text-good" : "text-primary")}>
                  {e.kind === "hit" ? "Hit" : e.kind === "heal" ? "Heal" : "Defensive"}</td>
                <td className="pr-3">{e.ability}{e.school && <span className="text-muted-foreground"> ({e.school})</span>}</td>
                <td className="pr-3 text-muted-foreground">{e.source || ""}</td>
                <td className="pr-3 tabular-nums">{e.kind === "defensive" ? "" : k(e.amount)}
                  {e.overkill ? <span className="text-muted-foreground"> ({k(e.overkill)} overkill)</span> : null}
                  {e.overheal ? <span className="text-muted-foreground"> ({k(e.overheal)} overheal)</span> : null}</td>
                <td>{e.hp == null ? "" : (
                  <span className="flex items-center gap-1.5 tabular-nums">
                    <span className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
                      <span className="block h-full bg-good" style={{ width: `${Math.round(e.hp * 100)}%` }} /></span>
                    {Math.round(e.hp * 100)}%</span>
                )}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!all && d.events.length > 8 && (
        <button className="self-start text-xs text-primary hover:underline" onClick={() => setAll(true)}>Show all {d.events.length} events</button>
      )}
    </>
  )
}

function ReadyCol({ title, items, owner }: { title: string; items: ReadyItem[]; owner?: boolean }) {
  const known = items.filter((i) => i.status !== "unknown" && !i.escape)
  const unknown = items.filter((i) => i.status === "unknown" && !i.escape)
  return (
    <div className="rounded-md border bg-card p-3">
      <h4 className="mb-2 text-xs font-semibold tracking-wider text-muted-foreground uppercase">{title}</h4>
      {known.length ? known.map((i) => (
        <div key={`${i.owner}-${i.name}`} className="mb-2 last:mb-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <b>{i.name}</b>{owner && <span className="text-xs text-muted-foreground">({i.owner})</span>}
            {i.verdict && (
              <Pill className={i.no_window ? "" : VERDICT_TONE[i.verdict]}>
                {VERDICT[i.verdict]}{i.verdict !== "died" && i.lowest != null ? ` ~${Math.round(i.lowest * 100)}%` : ""}
              </Pill>
            )}
            {i.no_window && <Pill title="No time to react: not telegraphed and they weren't low for 2s+ - not a mistake">No reaction window</Pill>}
            {i.gcd && <Pill title="Costs a global cooldown - holding it is often right">GCD</Pill>}
          </div>
          <div className="text-xs text-muted-foreground"><Tx>{i.text}</Tx></div>
        </div>
      )) : <p className="text-xs text-muted-foreground">Nothing tracked.</p>}
      {unknown.length > 0 && (
        <p className="mt-2 text-xs text-muted-foreground" title="Never pressed this run - may not be talented">
          Not pressed all run: {unknown.map((i) => i.name + (i.verdict ? ` (${VERDICT[i.verdict].toLowerCase()})` : "")).join(", ")}
        </p>
      )}
    </div>
  )
}

