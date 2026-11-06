import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { pullShort } from "@/lib/review"
import { Tx } from "@/lib/timeline"
import { LogRow } from "@/components/LogRow"
import { FindingParts, Part } from "@/components/FindingParts"
import { type Finding, sevOf } from "@/lib/review"
import { CLASS_COLORS } from "@/lib/wow"
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
const VERDICT_DOT = { lived: "bg-good", close: "bg-med", died: "bg-bad" }
const k = (v?: number | null) => (v == null ? "" : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : `${Math.round(v / 1000)}k`)
export const deathId = (clock: string, victim: string) => `death-${clock}-${victim}`

// A small status in text: an optional coloured dot, never a filled chip.
const Note = ({ dot, title, children }: { dot?: string; title?: string; children: React.ReactNode }) => (
  <span title={title} className={cn("inline-flex items-center gap-1.5 text-xs whitespace-nowrap", title && "cursor-help")}>
    {dot && <i className={cn("size-1.5 rounded-full", dot)} />}{children}
  </span>
)

// Each death: why it happened, what could have saved it (and from whom), and the last seconds of hits and heals.
export function Deaths({ deaths, player, classes = {}, findingFor }: {
  deaths: Death[]; player: string; classes?: Record<string, string | undefined>; findingFor: (d: Death) => Finding | null
}) {
  return (
    <section className="flex flex-col gap-4 border-t pt-7">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold tracking-tight">Death recaps</h2>
        <span className="text-sm text-muted-foreground tabular-nums">{deaths.length}</span>
      </div>
      {!deaths.length && <p className="text-sm text-muted-foreground">Nobody died.</p>}
      <div className="flex flex-col">
        {deaths.map((d) => { const f = findingFor(d); return (
          <LogRow key={deathId(d.clock, d.victim)} id={deathId(d.clock, d.victim)} time={d.clock} fix={f?.fix || d.fix}
                  mark={f ? { high: "bg-high", med: "bg-med", low: "bg-low" }[sevOf(f.severity)] : "bg-muted-foreground"}
                  title={<>
                    <span className="font-semibold" style={{ color: CLASS_COLORS[classes[d.victim] || ""] }}>{d.victim}</span>{" "}
                    <span className="font-medium">{(d.short || `died to ${d.killing}`).replace(/^Died/, "died")}</span>
                    <span className="ml-2 text-xs text-muted-foreground">{pullShort(d.pull)}</span>
                  </>}
                  extra={<>
                    {d.avoid && <Note dot="bg-med" title={d.avoid.line}>{d.avoid.label}</Note>}
                    {d.stoppable && <Note dot="bg-high" title="A cast peer groups usually stop did most of the damage">Stoppable</Note>}
                  </>}>
            <DeathView d={d} f={f} player={player} />
          </LogRow>
        ) })}
      </div>
    </section>
  )
}

const Story = ({ label, children, tone }: { label: string; children: React.ReactNode; tone?: string }) =>
  <Part label={label.replace(/:$/, "")} tone={tone}><p>{children}</p></Part>
const FRAME = "flex flex-col divide-y rounded-md border"

// One death, fully: the finding's verdict when the review flagged it (what was ready, damage, what happened, peers),
// then what only the recap knows - everyone's defensives with "would they have lived" and the last seconds of hits.
// Opened from "What to look at" or from the Death recaps list, it is the same view.
export function DeathView({ d, f, player }: { d: Death; f?: Finding | null; player: string }) {
  const [all, setAll] = useState(false)
  const events = all ? d.events : d.events.slice(-8)
  const s = d.stoppable
  if (!f?.parts) return <DeathBody d={d} player={player} />
  return (
    <FindingParts f={f}>
      {d.note && <Story label={`About ${d.cause?.name || d.killing}:`}>{d.note}</Story>}
      {d.avoid && <Story label={`${d.avoid.label}:`} tone="text-med">{d.avoid.line.split(" - ").slice(1).join(" - ")}</Story>}
      {s && (
        <Story label="Could likely have been stopped:" tone="text-high">
          {s.ability} did {Math.round(s.share * 100)}% of the damage, and peer groups stop that cast {Math.round(s.rate * 100)}% of the time
          ({s.kickable === false ? "it can't be interrupted - stun or CC only" : "interrupt, stun or CC"}). Ready then:{" "}
          {s.ready.length ? s.ready.map((x) => `${x.you ? "you" : x.player} - ${x.tools.join(", ")}`).join("; ") : "nothing - every interrupt and stun was on cooldown"}.
        </Story>
      )}
      <Evidence d={d} player={player} events={events} all={all} setAll={setAll} />
    </FindingParts>
  )
}

function DeathBody({ d, player }: { d: Death; player: string }) {
  const [all, setAll] = useState(false)
  const events = all ? d.events : d.events.slice(-8)
  const s = d.stoppable
  return (
    <dl className={FRAME}>
      {d.story?.advice.length ? (
        <Part label="What to do">
          <ul className="list-disc pl-5 marker:text-med">{d.story.advice.map((a, i) => <li key={i}><Tx>{a}</Tx></li>)}</ul>
        </Part>
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
        <Story label="Cause:"><Tx>{d.cause.lines.join("; ")}</Tx>{d.cause.name !== d.killing ? `; killing blow ${d.killing}` : ""}</Story>
      )}
      {!!d.story?.lines.length && (
        <Part label="What happened"><ul className="flex flex-col gap-1">{d.story.lines.map((l, i) => <li key={i}><Tx>{l}</Tx></li>)}</ul></Part>
      )}
      {!!d.last_casts?.filter((x) => x.target).length && (
        <Story label="Last casts:">
          {d.last_casts.filter((x) => x.target).map((x) => `${x.ability} → ${x.target}${x.hp != null ? ` @${Math.round(x.hp * 100)}%` : ""} (${x.dt.toFixed(1)}s)`).join("; ")}
        </Story>
      )}
      <Evidence d={d} player={player} events={events} all={all} setAll={setAll} />
    </dl>
  )
}

function Evidence({ d, player, events, all, setAll }: {
  d: Death; player: string; events: Death["events"]; all: boolean; setAll: (v: boolean) => void
}) {
  return (
    <>
      <Part label="Defensives - would they have lived?" wide>
      <div className="grid gap-4 md:grid-cols-3 md:gap-0 md:divide-x">
        <ReadyCol title="Group" items={d.ready.group} owner />
        <ReadyCol title={d.victim === player ? `${d.victim} (you, died)` : `${d.victim} (died)`} items={d.ready.victim} />
        <ReadyCol title={`${player} (healer)`} items={d.ready.healer} />
      </div>
      </Part>
      <Part label="Last 10 seconds" wide>
      <p className="text-xs text-muted-foreground">Took {k(d.taken)}, healed {k(d.healed)} (max HP {k(d.max_hp)}).</p>
      <LastSeconds d={d} />
      <Collapsible>
      <CollapsibleTrigger className="group flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
        <ChevronRight className="size-3 transition-transform group-data-[panel-open]:rotate-90" />Show every hit and heal
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 flex flex-col gap-2">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-muted-foreground"><tr className="border-b text-left">
            <th className="py-1 pr-3 font-medium">Before death</th><th className="pr-3" /><th className="pr-3 font-medium">Ability</th>
            <th className="pr-3 font-medium">From</th><th className="pr-3 font-medium">Amount</th><th className="font-medium">HP after</th>
          </tr></thead>
          <tbody>
            {events.map((e, i) => (
              <tr key={i} className="border-b border-border/50 last:border-0">
                <td className="py-1 pr-3 tabular-nums">{e.dt.toFixed(1)}s</td>
                <td className={cn("pr-3 font-semibold", e.kind === "hit" ? "text-bad" : e.kind === "heal" ? "text-good" : "text-foreground")}>
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
        <button className="self-start text-xs underline underline-offset-2 hover:no-underline" onClick={() => setAll(true)}>Show all {d.events.length} events</button>
      )}
      </CollapsibleContent>
      </Collapsible>
      </Part>
    </>
  )
}

function ReadyCol({ title, items, owner }: { title: string; items: ReadyItem[]; owner?: boolean }) {
  const known = items.filter((i) => i.status !== "unknown" && !i.escape)
  const unknown = items.filter((i) => i.status === "unknown" && !i.escape)
  return (
    <div className="md:px-4 md:first:pl-0 md:last:pr-0">
      <h4 className="mb-2 text-xs font-medium text-muted-foreground">{title}</h4>
      {known.length ? known.map((i) => (
        <div key={`${i.owner}-${i.name}`} className="mb-2 last:mb-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <b>{i.name}</b>{owner && <span className="text-xs text-muted-foreground">({i.owner})</span>}
            {i.verdict && (
              <Note dot={i.no_window ? "bg-muted-foreground" : VERDICT_DOT[i.verdict]}>
                {VERDICT[i.verdict]}{i.verdict !== "died" && i.lowest != null ? ` ~${Math.round(i.lowest * 100)}%` : ""}
              </Note>
            )}
            {i.no_window && <Note title="No time to react: not telegraphed and they weren't low for 2s+ - not a mistake"><span className="text-muted-foreground underline decoration-dotted">no reaction window</span></Note>}
            {i.gcd && <Note title="Costs a global cooldown - holding it is often right"><span className="text-muted-foreground underline decoration-dotted">GCD</span></Note>}
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

// The 10 seconds before the death: the victim's HP as a line, every hit (red, below) and heal (green, above) as a bar
// sized by its share of max HP, defensives as diamonds. Hover any mark for what it was.
function LastSeconds({ d }: { d: Death }) {
  const W = 640, H = 120, top = 8, hpH = 64, laneY = top + hpH + 26, lane = 18, maxHp = d.max_hp || 1
  const x = (dt: number) => ((Math.max(-10, dt) + 10) / 10) * W
  const y = (hp: number) => top + (1 - hp) * hpH
  const pts = d.events.filter((e) => e.hp != null).map((e) => `${x(e.dt).toFixed(1)},${y(e.hp!).toFixed(1)}`)
  const size = (a?: number) => Math.max(2, Math.min(lane, ((a || 0) / maxHp) * lane * 2.5))
  const tip = (e: Death["events"][number]) => `${e.dt.toFixed(1)}s ${e.kind === "hit" ? "Hit" : e.kind === "heal" ? "Heal" : "Defensive"}: ${e.ability}`
    + (e.source ? ` (${e.source})` : "") + (e.amount ? `, ${k(e.amount)}` : "") + (e.hp != null ? `, HP after ${Math.round(e.hp * 100)}%` : "")
  return (
    <figure className="flex flex-col gap-1">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-3xl overflow-visible" role="img"
           aria-label={`${d.victim}'s HP over the last 10 seconds before dying, with each hit and heal`}>
        {[0, 0.5, 1].map((v) => <line key={v} x1={0} x2={W} y1={y(v)} y2={y(v)} className="stroke-border" strokeWidth={1} />)}
        {[0.5, 1].map((v) => <text key={v} x={-6} y={y(v) + 4} textAnchor="end" className="fill-muted-foreground text-[10px]">{v * 100}%</text>)}
        <polyline points={pts.join(" ")} fill="none" className="stroke-data" strokeWidth={2} strokeLinejoin="round" />
        {d.events.filter((e) => e.hp != null).map((e, i) => (
          <circle key={i} cx={x(e.dt)} cy={y(e.hp!)} r={2.5} className="fill-data"><title>{tip(e)}</title></circle>
        ))}
        <line x1={0} x2={W} y1={laneY} y2={laneY} className="stroke-border" strokeWidth={1} />
        {d.events.map((e, i) => e.kind === "defensive" ? (
          <rect key={i} x={x(e.dt) - 4} y={top - 4} width={8} height={8} transform={`rotate(45 ${x(e.dt)} ${top})`} className="fill-foreground"><title>{tip(e)}</title></rect>
        ) : (
          <rect key={i} x={x(e.dt) - 1.5} width={3} rx={1}
                y={e.kind === "heal" ? laneY - size(e.amount) : laneY} height={size(e.amount)}
                className={e.kind === "heal" ? "fill-good" : "fill-bad"}><title>{tip(e)}</title></rect>
        ))}
        <text x={W} y={y(0) + 4} textAnchor="end" dx={10} className="fill-bad text-[12px] font-bold">✕</text>
        {[-10, -5, 0].map((t) => <text key={t} x={x(t)} y={H} textAnchor={t === -10 ? "start" : t === 0 ? "end" : "middle"} className="fill-muted-foreground text-[10px]">{t}s</text>)}
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><i className="h-0.5 w-4 rounded-full bg-data" />HP</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-1 rounded-sm bg-bad" />hit (below)</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-1 rounded-sm bg-good" />heal (above)</span>
        <span className="flex items-center gap-1.5"><i className="size-2 rotate-45 bg-foreground" />defensive</span>
        <span>Bar height = share of max HP. Hover a mark for details.</span>
      </figcaption>
    </figure>
  )
}
