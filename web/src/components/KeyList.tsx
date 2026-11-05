import { useCallback, useEffect, useRef, useState } from "react"
import { Check, ChevronRight, LoaderCircle, PanelLeftClose, RefreshCw } from "lucide-react"
import { api, type Recent, type Run } from "@/lib/api"
import { lsGet, lsSet, playSessions, RESULT_LABEL, result } from "@/lib/wow"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const STRIPE = { timed: "bg-good", untimed: "bg-warn", abandoned: "bg-bad" }

// Your recent keys, grouped into play sessions. The last saved list shows at once (no Warcraft Logs call), then the
// live one replaces it; when Warcraft Logs can't answer, the saved list stays with a note.
export function KeyList({ selected, onPick, version, onHide }: { selected?: string; onPick: (r: Run) => void; version: number; onHide?: () => void }) {
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
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 px-3 pt-3 pb-1">
        <h2 className="mr-auto text-sm font-medium">Your keys</h2>
        <Button variant="ghost" size="icon-sm" onClick={() => load(true)} disabled={loading} aria-label="Refresh key list"
                title="Check Warcraft Logs for new keys">
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />
        </Button>
        {onHide && (
          <Button variant="ghost" size="icon-sm" onClick={onHide} aria-label="Hide your keys" title="Hide your keys">
            <PanelLeftClose className="size-4" />
          </Button>
        )}
      </div>
      {note && <p className="px-3 pb-1 text-xs text-muted-foreground">{note}</p>}
      {error && !data && (
        <div className="m-3 rounded-lg border p-4 text-sm">
          <p className="mb-3">Couldn't load your recent keys: {error}</p>
          <Button size="sm" variant="outline" onClick={() => load()}>Try again</Button>
        </div>
      )}
      {!data && !error && <div className="flex flex-col gap-1.5 p-3">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-11 w-full" />)}</div>}
      {data && !data.runs.length && <p className="px-3 text-sm text-muted-foreground">No Mythic+ keys in your recent logs. Paste a log URL above to review one.</p>}
      {data && <Sessions runs={data.runs} selected={selected} onPick={onPick} />}
      {data?.more && !data.partial && limit < 12 && (
        <Button variant="ghost" size="sm" className="mx-3 mb-3 text-muted-foreground" onClick={() => setLimit(12)}>Show older keys</Button>
      )}
    </div>
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
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 py-2">
      {playSessions(runs).map((s, i) => (
        <Collapsible key={s.id} open={open[s.id] ?? i === 0} onOpenChange={(v) => toggle(s.id, v)}>
          <CollapsibleTrigger className="group flex w-full items-baseline gap-2 rounded-md px-1.5 py-1 text-left text-xs whitespace-nowrap hover:bg-accent"
                              title={`${s.count}, ${s.timed} timed`}>
            <ChevronRight className="size-3 shrink-0 self-center text-muted-foreground transition-transform group-data-[panel-open]:rotate-90" />
            <span className="font-medium">{s.day}</span>
            <span className="truncate text-muted-foreground">{s.hours}</span>
            <span className="ml-auto text-muted-foreground">{s.count}</span>
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col pt-0.5">
            {s.runs.map((r) => <KeyRow key={r.url} run={r} active={r.url === selected} onPick={onPick} />)}
          </CollapsibleContent>
        </Collapsible>
      ))}
    </div>
  )
}

// One key: the level as a numeral, the dungeon, and the result as a coloured stripe (green timed, amber over time,
// red abandoned) - the word is there too, for anyone who can't tell the colours apart.
function KeyRow({ run: r, active, onPick }: { run: Run; active: boolean; onPick: (r: Run) => void }) {
  const res = result(r)
  return (
    <button
      disabled={!r.analyzable}
      onClick={() => onPick(r)}
      aria-current={active || undefined}
      title={r.analyzable ? undefined : `You played ${r.spec || "another character"} - only supported healer specs can be analyzed`}
      className={cn(
        "relative grid grid-cols-[2.5rem_1fr_auto] items-center gap-x-2 rounded-md py-1.5 pr-2 pl-3 text-left",
        "hover:bg-accent disabled:cursor-default disabled:opacity-45 disabled:hover:bg-transparent",
        active && "bg-accent",
      )}
    >
      <span className={cn("absolute inset-y-2 left-0.5 w-[3px] rounded-full", STRIPE[res])} />
      <span className="row-span-2 text-xl leading-none font-semibold tracking-tight">
        <span className="text-sm font-normal text-muted-foreground">+</span>{r.level}
      </span>
      <span className={cn("truncate text-sm", active ? "font-semibold" : "font-medium")}>{r.dungeon}</span>
      <span className="row-span-2 self-center text-muted-foreground">
        {r.analyzed ? <Check className="size-3.5" aria-label="Analyzed" />
          : r.queued ? <LoaderCircle className="size-3.5 animate-spin" aria-label="Preparing" /> : null}
      </span>
      <span className="truncate text-xs text-muted-foreground">
        {RESULT_LABEL[res]}, {r.duration_min} min{!r.analyzable && `, ${r.spec || "not in group"}`}
      </span>
    </button>
  )
}
