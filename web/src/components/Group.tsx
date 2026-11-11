import type { Analysis, Meter } from "@/lib/analysis"
import { CLASS_COLORS, parseColor } from "@/lib/wow"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

// The group: compact player cards, the group / external cooldowns they brought as one row of chips, and the
// group's damage and healing over the key.
export function Group({ res, part }: { res: Analysis; part: "people" | "meters" }) {
  const brings = res.group.flatMap((m) => (m.brings || []).map((b) => ({ m, b })))
  if (part === "meters") {
    return res.meters ? (
      <div className="flex flex-col gap-5">
        <Chart label="DPS" rows={res.meters.damage} />
        <Chart label="HPS" rows={res.meters.healing} />
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
      {brings.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm">
          <span className="text-xs font-medium text-muted-foreground">Group cooldowns</span>
          {brings.map(({ m, b }) => {
            const group = b.scope === "group", big = m.big_hits || 0
            const missed = !b.casts || (group && !b.on_big_hits && big)
            const text = !b.casts ? "never pressed" : !group ? `${b.casts}x` : !b.on_big_hits && big
              ? `${b.casts}x, never on a big hit` : `${b.casts}x, ${b.on_big_hits} big hit${b.on_big_hits === 1 ? "" : "s"}`
            return (
              <Tooltip key={`${m.name}-${b.name}`}>
                <TooltipTrigger render={<span className="inline-flex cursor-help items-baseline gap-1.5" />}>
                  <span className="font-medium">{b.name}</span>
                  <span className="text-xs" style={{ color: CLASS_COLORS[m.class || ""] }}>{m.name}</span>
                  <span className={cn("text-xs", missed ? "font-medium text-warn" : "text-muted-foreground")}>{text}</span>
                </TooltipTrigger>
                <TooltipContent>
                  {group ? "Group" : "External"} cooldown, pressed {b.casts}x, up for {b.on_big_hits} of {big} big group hits
                  {b.casts ? "" : " (may not be talented)"}
                </TooltipContent>
              </Tooltip>
            )
          })}
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
