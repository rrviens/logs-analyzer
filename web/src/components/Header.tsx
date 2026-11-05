import { useEffect, useState } from "react"
import { Check, ChevronDown, FlaskConical, Gauge, HeartPulse, LogOut, Monitor, Moon, Settings2, Sun } from "lucide-react"
import { api, type Char, type Me, type MyChars, type Settings } from "@/lib/api"
import type { ThemePick } from "@/lib/theme"
import { CLASS_COLORS, lsGet, lsSet } from "@/lib/wow"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"

const charKey = (c: Char | Settings) => `${c.name}|${c.server}|${c.region}`.toLowerCase()
// Same list as the classic page ("recentChars2"), so recently used characters show in both.
const recentChars = (): Char[] => { try { return JSON.parse(lsGet("recentChars2") || "[]") } catch { return [] } }

type Props = { me: Me; settings: Settings; theme: ThemePick; setTheme: (p: ThemePick) => void; onCharacter: () => void; current?: string }

export function Header({ me, settings, theme, setTheme, onCharacter, current }: Props) {
  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-sidebar px-4">
      <a href="/next/" className="flex items-center gap-2 text-sm font-semibold">
        <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground"><HeartPulse className="size-4" /></span>
        <span className="max-sm:sr-only">Healer Review</span>
      </a>
      {current && <span className="hidden min-w-0 items-center gap-3 md:flex"><span className="text-muted-foreground/60">/</span><span className="truncate text-sm text-muted-foreground">{current}</span></span>}
      <span className="mr-auto" />
      {(me.mode === "local" || me.user) && <>
        <Budget />
        <Study />
        <CharacterPicker settings={settings} onCharacter={onCharacter} />
      </>}
      <DisplayMenu theme={theme} setTheme={setTheme} />
      {me.user && (
        <Button variant="ghost" size="sm" title={`Your Warcraft Logs user id: ${me.user_id}`}
                onClick={async () => { await fetch("/logout", { method: "POST" }); location.href = "/next/" }}>
          <LogOut className="size-4" />{me.user}
        </Button>
      )}
    </header>
  )
}

function CharacterPicker({ settings, onCharacter }: { settings: Settings; onCharacter: () => void }) {
  const [mine, setMine] = useState<MyChars | null>(null)
  const [manual, setManual] = useState(false)
  const [error, setError] = useState("")
  const current = settings.name ? settings : null

  const choose = async (c: Char) => {
    setError("")
    try {
      const saved = await api<Settings>("/api/settings", { method: "POST", body: JSON.stringify({ name: c.name, server: c.server, region: c.region }) })
      lsSet("recentChars2", JSON.stringify([{ ...c, ...saved }, ...recentChars().filter((r) => charKey(r) !== charKey(c))].slice(0, 5)))
      setManual(false)
      onCharacter()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setManual(true)
    }
  }

  // Asked again on each open when Warcraft Logs couldn't answer (the server then sends the last list it had).
  const onOpen = (open: boolean) => {
    if (open && (!mine || mine.busy)) api<MyChars>("/api/my-characters").then(setMine).catch(() => setMine({ characters: [], busy: true }))
  }
  const seen = new Set(current ? [charKey(current)] : [])
  const pick = (list: Char[]) => list.filter((c) => !seen.has(charKey(c)) && seen.add(charKey(c)))
  const groups: [string, Char[]][] = [["Your characters", pick(mine?.characters || [])], ["Recent", pick(recentChars())]]

  return (
    <>
      <DropdownMenu onOpenChange={onOpen}>
        <DropdownMenuTrigger render={<Button variant="outline" size="sm" className="gap-2 rounded-full" />}>
            {current ? (
              <>
                <span className="font-semibold" style={{ color: CLASS_COLORS[current.class || ""] }}>{current.name}</span>
                <span className="text-muted-foreground">{current.server} {current.region?.toUpperCase()}</span>
              </>
            ) : "Set character"}
            <ChevronDown className="size-3.5 opacity-60" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          {groups.filter(([, l]) => l.length).map(([label, list]) => (
            <div key={label}>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">{label}</div>
              {list.map((c) => (
                <DropdownMenuItem key={charKey(c)} onClick={() => choose(c)} className="gap-2.5">
                  <span className="h-6 w-1 rounded-full" style={{ background: CLASS_COLORS[c.class || ""] || "var(--border)" }} />
                  <span className="flex flex-col">
                    <span className="font-semibold" style={{ color: CLASS_COLORS[c.class || ""] }}>{c.name}</span>
                    <span className="text-xs text-muted-foreground">{c.server} · {c.region.toUpperCase()}{c.level ? ` · ${c.level}` : ""}</span>
                  </span>
                </DropdownMenuItem>
              ))}
            </div>
          ))}
          {mine?.busy && !mine.characters.length && (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">Warcraft Logs is busy - your characters will show when it answers.</p>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={() => setManual(true)}>Enter manually...</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <ManualCharacter open={manual} onOpenChange={setManual} initial={current} error={error} onSave={choose} />
    </>
  )
}

function ManualCharacter({ open, onOpenChange, initial, error, onSave }: {
  open: boolean; onOpenChange: (v: boolean) => void; initial: Settings | null; error: string; onSave: (c: Char) => void
}) {
  const [name, setName] = useState(initial?.name || "")
  const [server, setServer] = useState(initial?.server || "")
  const [region, setRegion] = useState(initial?.region || "eu")
  // Realm autocomplete: a hand-kept list per region (data/realms.json); any name still works as typed.
  const [realms, setRealms] = useState<Record<string, string[]> | null>(null)
  useEffect(() => { if (open && !realms) api<Record<string, string[]>>("/api/realms").then(setRealms).catch(() => setRealms({})) }, [open, realms])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Your character</DialogTitle></DialogHeader>
        <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); onSave({ name, server, region }) }}>
          <Input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
          <Input placeholder="Realm, e.g. Kazzak" value={server} onChange={(e) => setServer(e.target.value)} required list="realms" autoComplete="off" />
          <datalist id="realms">{(realms?.[region] || []).map((r) => <option key={r} value={r} />)}</datalist>
          <select className="h-9 rounded-md border bg-transparent px-3 text-sm" value={region} onChange={(e) => setRegion(e.target.value)}>
            {["eu", "us", "kr", "tw"].map((r) => <option key={r} value={r}>{r.toUpperCase()}</option>)}
          </select>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <DialogFooter><Button type="submit">Save</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// Text size: Normal / Large (the page is in rem); same setting as the classic page.
export const applyTextSize = (large: boolean) => document.documentElement.classList.toggle("large", large)

// Display: theme and text size, from the icon at the header's edge.
function DisplayMenu({ theme, setTheme }: { theme: ThemePick; setTheme: (p: ThemePick) => void }) {
  const [large, setLarge] = useState(lsGet("textSize") === "large")
  const size = (l: boolean) => { lsSet("textSize", l ? "large" : "normal"); applyTextSize(l); setLarge(l) }
  const opts: [ThemePick, string, typeof Sun][] = [["system", "System", Monitor], ["dark", "Dark", Moon], ["light", "Light", Sun]]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon" aria-label="Display: theme and text size" />}>
        <Settings2 className="size-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <div className="px-2 py-1.5 text-xs text-muted-foreground">Theme</div>
        {opts.map(([k, label, I]) => (
          <DropdownMenuItem key={k} onClick={() => setTheme(k)} className="gap-2">
            <I className="size-4" />{label}{theme === k && <Check className="ml-auto size-4" />}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <div className="px-2 py-1.5 text-xs text-muted-foreground">Text size</div>
        {([[false, "Normal"], [true, "Large"]] as const).map(([l, label]) => (
          <DropdownMenuItem key={label} onClick={() => size(l)} className="gap-2">
            {label}{large === l && <Check className="ml-auto size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

type BudgetInfo = { used: number; limit: number; reset_in_s: number }
// Warcraft Logs API points used this hour (your own account's limit when signed in).
function Budget() {
  const [b, setB] = useState<BudgetInfo | null>(null)
  useEffect(() => {
    const load = () => api<BudgetInfo>("/api/budget").then(setB).catch(() => setB(null))
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [])
  if (!b) return null
  const share = b.used / Math.max(1, b.limit)
  return (
    <span className="hidden items-center gap-1.5 text-xs text-muted-foreground lg:flex" title="Warcraft Logs API points used this hour">
      <Gauge className={cn("size-3.5", share > 0.8 ? "text-bad" : share > 0.5 ? "text-warn" : "")} />
      WCL API {b.used.toLocaleString()}/{b.limit.toLocaleString()} · resets in {Math.ceil(b.reset_in_s / 60)} min
    </span>
  )
}

type StudyInfo = { exists?: boolean; generated?: string; runs?: Record<string, number>; can_run?: boolean
                   job?: { status?: string; progress?: string[]; error?: string } | null
                   covered?: { key: string; label: string; generated?: string }[]; specs?: { key: string; label: string }[] }
const daysAgo = (iso?: string) => (iso ? Math.floor((Date.now() - new Date(iso).getTime()) / 86400000) : null)

// The top-player study the review compares against: its age on the chip; details (and, for admins, a refresh) on click.
function Study() {
  const [s, setS] = useState<StudyInfo | null>(null)
  const [open, setOpen] = useState(false)
  const [spec, setSpec] = useState("resto_shaman")
  const [runs, setRuns] = useState(5)
  const [error, setError] = useState("")
  const running = s?.job?.status === "running"
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    const load = () => api<StudyInfo>("/api/study").then((d) => {
      setS(d)
      if (d.job?.status === "running") timer = setTimeout(load, 5000)
    }).catch(() => {})
    load()
    return () => clearTimeout(timer)
  }, [running])
  if (!s) return null
  const days = daysAgo(s.generated), counts = Object.values(s.runs || {})
  const label = running ? `Study running: ${s.job?.progress?.at(-1) || "starting..."}`
    : s.job?.status === "error" ? "Study failed"
    : !s.exists ? "No top-player study"
    : `Study ${days == null ? "age unknown" : days === 0 ? "today" : `${days}d old`}${counts.length ? ` · ${Math.min(...counts)}-${Math.max(...counts)} runs/dungeon` : ""}`
  const stale = !running && (days == null || days >= 7)
  const ago = (iso?: string) => { const d = daysAgo(iso); return d == null ? "never" : d === 0 ? "today" : d === 1 ? "yesterday" : `${d} days ago` }
  const start = async () => {
    setError("")
    try { await api("/api/study", { method: "POST", body: JSON.stringify({ spec, runs }) }); setOpen(false); setS({ ...s, job: { status: "running" } }) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  return (
    <>
      <Button variant="outline" size="sm" className={cn("max-w-80 gap-1.5 rounded-full max-sm:hidden", stale && "border-warn/50 text-warn")}
              title={s.job?.error || "The top-player study the analysis compares against"} onClick={() => setOpen(true)}>
        <FlaskConical className="size-3.5" /><span className="truncate">{label}</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Top-player study</DialogTitle>
            <DialogDescription>The season's top-ranked runs per dungeon: timings, habits and the Top players band.
              {s.can_run ? "" : " It's refreshed by the site admin."}</DialogDescription>
          </DialogHeader>
          <p className="text-sm"><b>Specs covered:</b> {s.covered?.map((c) => `${c.label} (${ago(c.generated)})`).join(", ") || "none yet"}</p>
          {running && <div className="rounded-md bg-secondary p-2 text-xs text-muted-foreground">{(s.job?.progress || []).slice(-6).map((x, i) => <div key={i}>{x}</div>)}</div>}
          {s.can_run && !running && (
            <div className="grid gap-2 text-sm">
              <label className="grid gap-1">Spec
                <select className="h-9 rounded-md border bg-transparent px-3" value={spec} onChange={(e) => setSpec(e.target.value)}>
                  {s.specs?.map((sp) => <option key={sp.key} value={sp.key}>{sp.label}</option>)}
                </select></label>
              <label className="grid gap-1">Top runs per dungeon
                <Input type="number" min={3} max={20} value={runs} onChange={(e) => setRuns(+e.target.value || 5)} /></label>
              <p className="text-xs text-muted-foreground">It pauses for the hourly Warcraft Logs reset when the budget runs low (keeping some
                for everyone's analyses), so it can take hours. Cached runs make a rerun cheap.</p>
              {error && <p className="text-destructive">{error}</p>}
              <DialogFooter>
                <Button variant="outline" onClick={() => { location.href = `/api/study/export?spec=${spec}` }}>Export file</Button>
                <Button onClick={start}>Start study</Button>
              </DialogFooter>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
