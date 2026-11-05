import type { Analysis, Meter } from "@/lib/analysis"
import { CLASS_COLORS } from "@/lib/wow"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

// The group: compact player cards, the group / external cooldowns they brought as one row of chips, and the
// group's damage and healing over the key.
export function Group({ res }: { res: Analysis }) {
  const brings = res.group.flatMap((m) => (m.brings || []).map((b) => ({ m, b })))
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {res.group.map((m) => (
          <div key={m.name} className="flex gap-2.5 overflow-hidden rounded-lg border bg-secondary py-2 pr-2">
            <span className="w-1 shrink-0 rounded-r" style={{ background: CLASS_COLORS[m.class || ""] || "var(--border)" }} />
            <div className="min-w-0">
              <div className="text-[0.7rem] font-medium tracking-wider text-muted-foreground uppercase">{m.role}</div>
              <div className="truncate font-semibold">{m.name}</div>
              <div className="truncate text-xs text-muted-foreground">{[m.spec, m.class].filter(Boolean).join(" ")}</div>
              {m.note && <div className="text-xs text-med">{m.note}</div>}
            </div>
          </div>
        ))}
      </div>
      {brings.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="mr-1 font-semibold text-muted-foreground">Group cooldowns</span>
          {brings.map(({ m, b }) => {
            const group = b.scope === "group", big = m.big_hits || 0
            const missed = !b.casts || (group && !b.on_big_hits && big)
            const text = !b.casts ? "never pressed" : !group ? `${b.casts}x` : !b.on_big_hits && big
              ? `${b.casts}x, never on a big hit` : `${b.casts}x · ${b.on_big_hits} big hit${b.on_big_hits === 1 ? "" : "s"}`
            return (
              <Tooltip key={`${m.name}-${b.name}`}>
                <TooltipTrigger render={<span className={cn("inline-flex cursor-help items-center gap-1.5 rounded-full border bg-secondary px-2.5 py-1",
                                                           missed && "border-warn/50")} />}>
                  <span className="size-1.5 rounded-full" style={{ background: CLASS_COLORS[m.class || ""] }} />
                  <b>{b.name}</b>
                  <span className={missed ? "font-semibold text-warn" : "text-muted-foreground"}>{m.name} · {text}</span>
                </TooltipTrigger>
                <TooltipContent>
                  {group ? "Group" : "External"} cooldown · pressed {b.casts}x · up for {b.on_big_hits} of {big} big group hits
                  {b.casts ? "" : " · may not be talented"}
                </TooltipContent>
              </Tooltip>
            )
          })}
        </div>
      )}
      {res.comp_notes?.map((n) => <p key={n} className="text-xs text-muted-foreground">{n}</p>)}
      {res.meters && (
        <div className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
          <Chart label="DPS" rows={res.meters.damage} />
          <Chart label="HPS" rows={res.meters.healing} />
        </div>
      )}
    </div>
  )
}

const k = (x: number) => (x >= 1e6 ? `${(x / 1e6).toFixed(1)}M` : `${Math.round(x / 1000)}k`)

function Chart({ label, rows }: { label: string; rows: Meter[] }) {
  const top = Math.max(1, ...rows.map((e) => e.per_s))
  return (
    <div className="grid grid-cols-[max-content_1fr_max-content] items-center gap-x-2.5 gap-y-1.5 text-sm">
      <div className="col-span-3 text-xs font-semibold tracking-wider text-muted-foreground">{label}</div>
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
      <span className="h-1.5 overflow-hidden rounded-full bg-secondary">
        <span className="block h-full rounded-full" style={{ width: `${Math.max(1, pct)}%`, background: CLASS_COLORS[e.class] || "var(--border)" }} />
      </span>
      <span className="text-right text-muted-foreground tabular-nums" title={`${k(e.total)} total`}>{k(e.per_s)}</span>
    </>
  )
}
