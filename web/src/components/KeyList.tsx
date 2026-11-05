import { useCallback, useEffect, useRef, useState } from "react"
import { ChevronRight, RefreshCw } from "lucide-react"
import { api, type Recent, type Run } from "@/lib/api"
import { ago, lsGet, lsSet, playSessions, RESULT_LABEL, result } from "@/lib/wow"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const RESULT_TONE = { timed: "text-good bg-good/12", untimed: "text-warn bg-warn/12", abandoned: "text-bad bg-bad/12" }

// Your recent keys, grouped into play sessions. The last saved list shows at once (no Warcraft Logs call), then the
// live one replaces it; when Warcraft Logs can't answer, the saved list stays with a note.
export function KeyList({ selected, onPick, version }: { selected?: string; onPick: (r: Run) => void; version: number }) {
  const [data, setData] = useState<Recent | null>(null)
  const [note, setNote] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  const [limit, setLimit] = useState(5)
  const have = useRef(false)

  const load = useCallback(async (fresh = false) => {
    setLoading(true)
    setError("")
    if (!have.current) {
      const saved = await api<Recent>("/api/recent?saved=true").catch(() => null)
      if (saved?.runs.length) {
        setData(saved)
        have.current = true
        setNote("Saved list - checking Warcraft Logs for new keys...")
      }
    }
    try {
      const live = await api<Recent>(`/api/recent?limit=${limit}${fresh ? "&fresh=true" : ""}`)
      setData(live)
      have.current = true
      setNote(live.note || (live.partial ? "Some keys couldn't be loaded yet." : ""))
    } catch (e) {
      if (have.current) setNote("Couldn't reach Warcraft Logs - showing your saved list.")
      else setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => { load() }, [load, version])

  // Keys being prepared in the background: look again until they're done.
  useEffect(() => {
    if (!data?.runs.some((r) => r.queued)) return
    const t = setTimeout(() => load(), 20000)
    return () => clearTimeout(t)
  }, [data, load])

  return (
    <aside className="flex min-h-0 flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold tracking-wider text-muted-foreground uppercase">Your recent keys</h2>
        <Button variant="ghost" size="icon" onClick={() => load(true)} disabled={loading} aria-label="Refresh key list"
                title="Check Warcraft Logs for new keys">
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />
        </Button>
      </div>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {error && !data && (
        <div className="rounded-lg border p-4 text-sm">
          <p className="mb-3">Couldn't load your recent keys: {error}</p>
          <Button size="sm" variant="outline" onClick={() => load()}>Try again</Button>
        </div>
      )}
      {!data && !error && Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-14 w-full" />)}
      {data && !data.runs.length && <p className="text-sm text-muted-foreground">No recent M+ runs found.</p>}
      {data && <Sessions runs={data.runs} selected={selected} onPick={onPick} />}
      {data?.more && !data.partial && (
        <Button variant="outline" size="sm" onClick={() => setLimit(12)} disabled={limit >= 12}>Load more</Button>
      )}
    </aside>
  )
}

function Sessions({ runs, selected, onPick }: { runs: Run[]; selected?: string; onPick: (r: Run) => void }) {
  const [open, setOpen] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(lsGet("keySessions") || "{}") } catch { return {} }
  })
  const toggle = (id: string, v: boolean) => {
    const next = { ...open, [id]: v }
    setOpen(next)
    lsSet("keySessions", JSON.stringify(next))
  }
  return (
    <div className="flex flex-col gap-2 overflow-y-auto pr-1">
      {playSessions(runs).map((s, i) => (
        <Collapsible key={s.id} open={open[s.id] ?? i === 0} onOpenChange={(v) => toggle(s.id, v)}>
          <CollapsibleTrigger className="group flex w-full items-center gap-1.5 py-1 text-left text-xs font-medium text-muted-foreground hover:text-foreground">
            <ChevronRight className="size-3.5 transition-transform group-data-[panel-open]:rotate-90" />
            {s.label}
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-1.5 pt-1">
            {s.runs.map((r) => <KeyRow key={r.url} run={r} active={r.url === selected} onPick={onPick} />)}
          </CollapsibleContent>
        </Collapsible>
      ))}
    </div>
  )
}

function KeyRow({ run: r, active, onPick }: { run: Run; active: boolean; onPick: (r: Run) => void }) {
  const res = result(r)
  return (
    <button
      disabled={!r.analyzable}
      onClick={() => onPick(r)}
      title={r.analyzable ? undefined : `You played ${r.spec || "another character"} - only supported healer specs can be analyzed`}
      className={cn(
        "grid grid-cols-[2.6rem_1fr_auto] items-center gap-x-3 gap-y-0.5 rounded-lg border bg-card px-3 py-2.5 text-left transition-colors",
        "hover:border-ring/50 hover:bg-accent disabled:cursor-default disabled:opacity-50 disabled:hover:bg-card",
        active && "border-primary bg-primary/10",
      )}
    >
      <span className="row-span-2 rounded-md border bg-background py-1.5 text-center text-sm font-bold tabular-nums">+{r.level}</span>
      <span className="truncate text-sm font-semibold">{r.dungeon}</span>
      <Badge variant="secondary" className={cn("justify-self-end border-0 font-semibold", RESULT_TONE[res])}>{RESULT_LABEL[res]}</Badge>
      <span className="flex gap-2 text-xs text-muted-foreground">
        <span>{ago(r.started)}</span><span>{r.duration_min} min</span>{!r.analyzable && <span>{r.spec || "not in group"}</span>}
      </span>
      <span className="justify-self-end text-xs">
        {r.analyzed ? <span className="text-med">Analyzed</span> : r.queued ? <span className="text-primary">Preparing...</span> : null}
      </span>
    </button>
  )
}
