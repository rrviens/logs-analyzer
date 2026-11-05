import { useState } from "react"
import { Check, ChevronDown, HeartPulse, LogOut, Monitor, Moon, Sun } from "lucide-react"
import { api, type Char, type Me, type MyChars, type Settings } from "@/lib/api"
import type { ThemePick } from "@/lib/theme"
import { CLASS_COLORS, lsGet, lsSet } from "@/lib/wow"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"

const charKey = (c: Char | Settings) => `${c.name}|${c.server}|${c.region}`.toLowerCase()
// Same list as the classic page ("recentChars2"), so recently used characters show in both.
const recentChars = (): Char[] => { try { return JSON.parse(lsGet("recentChars2") || "[]") } catch { return [] } }

type Props = { me: Me; settings: Settings; theme: ThemePick; setTheme: (p: ThemePick) => void; onCharacter: () => void }

export function Header({ me, settings, theme, setTheme, onCharacter }: Props) {
  return (
    <header className="flex flex-wrap items-center gap-3 border-b px-5 py-3">
      <a href="/next/" className="mr-auto flex items-center gap-2 font-semibold">
        <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground"><HeartPulse className="size-4.5" /></span>
        Healer Review
      </a>
      {(me.mode === "local" || me.user) && <CharacterPicker settings={settings} onCharacter={onCharacter} />}
      <ThemeMenu theme={theme} setTheme={setTheme} />
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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Your character</DialogTitle></DialogHeader>
        <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); onSave({ name, server, region }) }}>
          <Input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
          <Input placeholder="Server" value={server} onChange={(e) => setServer(e.target.value)} required />
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

function ThemeMenu({ theme, setTheme }: { theme: ThemePick; setTheme: (p: ThemePick) => void }) {
  const opts: [ThemePick, string, typeof Sun][] = [["system", "System", Monitor], ["dark", "Dark", Moon], ["light", "Light", Sun]]
  const Icon = opts.find(([k]) => k === theme)?.[2] || Monitor
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon" aria-label="Theme" />}>
        <Icon className="size-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {opts.map(([k, label, I]) => (
          <DropdownMenuItem key={k} onClick={() => setTheme(k)} className="gap-2">
            <I className="size-4" />{label}{theme === k && <Check className="ml-auto size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
