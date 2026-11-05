import { useEffect, useState } from "react"
import { ExternalLink, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import type { Analysis } from "@/lib/analysis"
import { CLASS_COLORS, RESULT_LABEL, type Result } from "@/lib/wow"
import { Button, buttonVariants } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type IoGroup = { avg: number | null; color?: string; pending?: boolean; with_score: number; total: number;
                 players: { name: string; class?: string; score?: number; known?: boolean }[] }
type Io = { group: IoGroup | null; top_avg?: number | null; pending: boolean }

const DOT = { timed: "bg-good", untimed: "bg-warn", abandoned: "bg-bad" }
const fmt = (n: number) => n.toLocaleString("en-US")
const IO_NOTE = "Raider.io current-season scores, not the scores at the time of the run."

// A chip in the run header: one shape for result / IO / peers, a coloured dot carries the meaning
// (dot: a Tailwind bg-* class or a CSS colour).
function Chip({ dot, children, className }: { dot?: string; children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex h-7 items-center gap-1.5 rounded-full border bg-secondary px-3 text-sm font-semibold tabular-nums", className)}>
      {dot && <span className={cn("size-2 rounded-full", dot.startsWith("bg-") && dot)}
                    style={dot.startsWith("bg-") ? undefined : { background: dot }} />}
      {children}
    </span>
  )
}

export function RunHeader({ res, url, onReanalyze, busy }: { res: Analysis; url: string; onReanalyze?: () => void; busy: boolean }) {
  const r = res.run
  const kind: Result = r.timed ? "timed" : r.duration_min < 20 ? "abandoned" : "untimed"
  const c = res.compared  // missing on the preview (your run alone) until the peer logs are in
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2.5">
        <h2 className="mr-1 text-2xl font-bold tracking-tight">{r.dungeon} +{r.level}</h2>
        <Chip dot={DOT[kind]}>{RESULT_LABEL[kind]}</Chip>
        <IoChips url={url} />
        <div className="ml-auto flex gap-2">
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href={url} target="_blank" rel="noopener">
            <ExternalLink />Open log
          </a>
          {onReanalyze && <Button variant="outline" size="sm" onClick={onReanalyze} disabled={busy}>
            <RefreshCw className={cn(busy && "animate-spin")} />Re-analyze
          </Button>}
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        {r.player} ({res.spec.label}) · {r.duration_min} min · {r.deaths} death{r.deaths === 1 ? "" : "s"} ·{" "}
        {c ? <>compared with {c.count} peer {res.spec.label} logs ({c.levels.map((l) => `+${l}`).join(", ") || "none"})
               {c.model ? ` · top players ${c.model}` : ""}</> : "comparing with peer logs..."}
      </p>
    </div>
  )
}

// Your group's Raider.io average, and the peer groups' with the gap. Scores fill in over a few seconds.
function IoChips({ url }: { url: string }) {
  const [io, setIo] = useState<Io | null>(null)
  useEffect(() => {
    let stop = false, tries = 0
    const load = async () => {
      const d = await api<Io>(`/api/io?url=${encodeURIComponent(url)}`).catch(() => null)
      if (stop) return
      setIo(d)
      if (d?.pending && tries++ < 12) setTimeout(load, 5000)
    }
    setIo(null)
    load()
    return () => { stop = true }
  }, [url])
  const g = io?.group
  if (!g || (!g.avg && !g.pending)) return null
  if (!g.avg) return <Chip><span className="font-medium text-muted-foreground">IO</span> ...</Chip>
  const part = g.with_score < g.total ? ` (${g.with_score} of ${g.total})` : ""
  const gap = io?.top_avg ? io.top_avg - g.avg : null
  return (
    <>
      <Tooltip>
        <TooltipTrigger render={<span className="cursor-help" />}>
          <Chip dot={g.color || "var(--primary)"}><span className="font-medium text-muted-foreground">IO</span> {fmt(g.avg)}{part}</Chip>
        </TooltipTrigger>
        <TooltipContent className="w-60">
          {g.players.map((p) => (
            <div key={p.name} className="flex justify-between gap-3">
              <span style={{ color: CLASS_COLORS[p.class || ""] }}>{p.name}</span>
              <b>{p.score ? fmt(p.score) : p.known ? "no score" : "..."}</b>
            </div>
          ))}
          <p className="mt-1.5 opacity-70">Your group's average. {IO_NOTE}</p>
        </TooltipContent>
      </Tooltip>
      {gap != null && (
        <Tooltip>
          <TooltipTrigger render={<span className="cursor-help" />}>
            <Chip><span className="font-medium text-muted-foreground">Peers</span> {fmt(io!.top_avg!)}
              <span className={cn(Math.abs(gap) < 100 ? "text-muted-foreground" : gap > 0 ? "text-high" : "text-good")}>
                ({gap > 0 ? "+" : ""}{fmt(Math.round(gap))})
              </span>
            </Chip>
          </TooltipTrigger>
          <TooltipContent className="w-64">
            Average Raider.io score of the peer groups your run is compared with - the best-ranked healers of your spec at
            your key level in this dungeon. {Math.abs(gap) < 100 ? "Your group is about the same." : `Your group is ${fmt(Math.abs(Math.round(gap)))} ${gap > 0 ? "lower" : "higher"}.`} {IO_NOTE}
          </TooltipContent>
        </Tooltip>
      )}
    </>
  )
}
