import type { Analysis, Meter } from "@/lib/analysis"
import { CLASS_COLORS, parseColor } from "@/lib/wow"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

// The group: compact player cards, the group / external cooldowns each player brought, and the
// group's damage and healing over the key.
export function Group({ res, part }: { res: Analysis; part: "people" | "meters" }) {
  if (part === "meters") {
    // The five players only: WCL's tables also list NPCs that heal or fight for the group (e.g. a dungeon ally)
    const players = new Set(res.group.map((m) => m.name)), mine = (rows: Meter[]) => rows.filter((e) => players.has(e.name))
    return res.meters ? (
      <div className="flex flex-col gap-5">
        <Chart label="DPS" rows={mine(res.meters.damage)} />
        <Chart label="HPS" rows={mine(res.meters.healing)} />
      </div>
    ) : null
  }
  return (
    <div className="flex flex-col gap-3">
      {/* The roster: name in class colour, role and spec quiet beneath - no boxes. */}
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 border-y py-3 sm:grid-cols-3 lg:grid-cols-5">
        {res.group.map((m) => (
          <div key={m.name} className="min-w-0">
            <div className="truncate font-semibold" style={{ color: CLASS_COLORS[m.class || ""] }}>{m.name}</div>
            <div className="truncate text-xs text-muted-foreground">
              <span className="capitalize">{m.role === "dps" ? "DPS" : m.role}</span>, {[m.spec, m.class].filter(Boolean).join(" ")}
            </div>
            {m.parses && (
              <div className="flex gap-3 text-xs text-muted-foreground" title="Warcraft Logs parse: percentile vs keys of this level">
                {(["dps", "hps"] as const).map((k) => m.parses?.[k] != null && (
                  <span key={k}>{k.toUpperCase()} <b className="tabular-nums" style={{ color: parseColor(m.parses[k]!) }}>{m.parses[k]}</b></span>
                ))}
              </div>
            )}
            {m.note && <div className="text-xs text-med">{m.note}</div>}
          </div>
        ))}
      </div>
      {/* Group / external cooldowns: a block per player who has any - name, then one short line per cooldown */}
      {res.group.some((m) => m.brings?.length) && (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Group cooldowns</span>
          <div className="flex flex-wrap gap-x-10 gap-y-3">
            {res.group.filter((m) => m.brings?.length).map((m) => (
              <div key={m.name} className="text-sm">
                <div className="font-medium" style={{ color: CLASS_COLORS[m.class || ""] }}>{m.name}</div>
                {m.brings!.map((b) => {
                  const group = b.scope === "group", big = m.big_hits || 0
                  const missed = !b.casts || (group && !b.on_big_hits && big)
                  const text = !b.casts ? "not pressed" : group && big ? `${b.casts}x, ${b.on_big_hits || "no"} big hit${b.on_big_hits === 1 ? "" : "s"}` : `${b.casts}x`
                  return (
                    <Tooltip key={b.name}>
                      <TooltipTrigger render={<div className="flex cursor-help items-baseline gap-2" />}>
                        <span>{b.name}</span>
                        <span className={cn("text-xs tabular-nums", missed ? "font-medium text-warn" : "text-muted-foreground")}>{text}</span>
                      </TooltipTrigger>
                      <TooltipContent>
                        {group ? "Group" : "External"} cooldown, pressed {b.casts}x, up for {b.on_big_hits} of {big} big group hits
                        {b.casts ? "" : " (may not be talented)"}
                      </TooltipContent>
                    </Tooltip>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
      )}
      {res.comp_notes?.map((n) => <p key={n} className="text-xs text-muted-foreground">{n}</p>)}
    </div>
  )
}

const k = (x: number) => (x >= 1e6 ? `${(x / 1e6).toFixed(1)}M` : `${Math.round(x / 1000)}k`)

function Chart({ label, rows }: { label: string; rows: Meter[] }) {
  const top = Math.max(1, ...rows.map((e) => e.per_s))
  return (
    <div className="grid grid-cols-[max-content_1fr_max-content] items-center gap-x-2.5 gap-y-1.5 text-sm">
      <div className="col-span-3 text-xs font-medium text-muted-foreground">{label}</div>
      {rows.map((e) => (
        <Row key={e.name} e={e} pct={(e.per_s / top) * 100} />
      ))}
    </div>
  )
}

function Row({ e, pct }: { e: Meter; pct: number }) {
  return (
    <>
      <span>{e.name}</span>
      <span className="h-2 overflow-hidden bg-secondary">
        <span className="block h-full" style={{ width: `${Math.max(1, pct)}%`, background: CLASS_COLORS[e.class] || "var(--border)" }} />
      </span>
      <span className="text-right tabular-nums">{k(e.total)} <span className="text-muted-foreground">({k(e.per_s)}/s)</span></span>
    </>
  )
}
