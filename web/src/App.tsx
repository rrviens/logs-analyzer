import { useEffect, useState } from "react"
import { PanelLeftClose, PanelLeftOpen } from "lucide-react"
import { api, type Me, type Run, type Settings } from "@/lib/api"
import { useTheme } from "@/lib/theme"
import { Header } from "@/components/Header"
import { KeyList } from "@/components/KeyList"
import { SignIn } from "@/components/SignIn"
import { Review } from "@/components/Review"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { Analysis } from "@/lib/analysis"
import { TooltipProvider } from "@/components/ui/tooltip"

export default function App() {
  const [theme, setTheme] = useTheme()
  const [me, setMe] = useState<Me | null>(null)
  const [settings, setSettings] = useState<Settings>({})
  const [version, setVersion] = useState(0)  // bumped when the character changes: everything reloads
  const [picked, setPicked] = useState<Run | null>(null)
  const [keysOpen, setKeysOpen] = useState(true)  // folds to a rail while a review is open, so the review gets the width
  const open = keysOpen || !picked

  const [shared, setShared] = useState<Analysis | null>(null)  // signed out: a saved analysis from a ?run= link
  const linked = new URLSearchParams(location.search).get("run")
  useEffect(() => { api<Me>("/api/me").then(setMe).catch(() => setMe({ mode: "local", user: null })) }, [])
  const signedIn = me && (me.mode === "local" || me.user)
  useEffect(() => {  // ?run=<log url>: signed in, open it; signed out, show its saved analysis if there is one
    if (!me || !linked) return
    if (signedIn) setPicked({ url: linked } as Run)
    else api<{ result: Analysis }>(`/api/public-analysis?url=${encodeURIComponent(linked)}`).then((d) => setShared(d.result)).catch(() => {})
  }, [me])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (signedIn) api<Settings>("/api/settings").then(setSettings).catch(() => {}) }, [signedIn, version])

  if (!me) return null
  return (
    <TooltipProvider>
      <div className="flex min-h-svh flex-col">
        <Header me={me} settings={settings} theme={theme} setTheme={setTheme}
                onCharacter={() => { setPicked(null); setVersion((v) => v + 1) }} />
        {!signedIn ? (shared && linked ? (
          <div className="mx-auto flex w-full max-w-[100rem] flex-col gap-4 p-5">
            <p className="flex flex-wrap items-center gap-3 rounded-lg border bg-secondary px-4 py-2.5 text-sm text-muted-foreground">
              Viewing a saved analysis of a public run. Sign in to analyze new runs or your private logs.
              <a href="/login" className="ml-auto font-medium text-primary hover:underline">Sign in with Warcraft Logs</a>
            </p>
            <ErrorBoundary><Review url={linked} saved={shared} /></ErrorBoundary>
          </div>
        ) : <SignIn />) : (
          <div className={`mx-auto grid w-full max-w-[100rem] flex-1 gap-5 p-5 transition-[grid-template-columns] duration-300 ease-out motion-reduce:transition-none ${open ? "md:grid-cols-[20rem_1fr]" : "md:grid-cols-[2.5rem_1fr]"}`}>
            {!settings.name ? <p className="text-sm text-muted-foreground">Set your character (top right) to see your recent keys.</p> : (
              // Both stay mounted: the column width animates while the list fades into the rail (and back).
              <aside className="relative min-w-0 md:overflow-x-clip">
                <div inert={!open} className={`flex w-full flex-col gap-2 transition-opacity duration-200 md:w-[20rem] ${open ? "opacity-100 delay-100" : "opacity-0 max-md:hidden"}`}>
                  {picked && (
                    <button className="flex items-center gap-1.5 self-end text-xs text-muted-foreground hover:text-foreground" onClick={() => setKeysOpen(false)}>
                      <PanelLeftClose className="size-4" />Hide keys
                    </button>
                  )}
                  <form className="flex gap-2" onSubmit={(e) => {
                    e.preventDefault()
                    const v = new FormData(e.currentTarget).get("url")?.toString().trim()
                    if (v) setPicked({ url: v } as Run)
                  }}>
                    <Input name="url" placeholder="Paste a Warcraft Logs URL" autoComplete="off" className="h-8 text-sm" />
                    <Button type="submit" size="sm" variant="secondary">Analyze</Button>
                  </form>
                  <KeyList selected={picked?.url} onPick={setPicked} version={version} />
                </div>
                <button inert={open} onClick={() => setKeysOpen(true)} title="Show your keys" aria-label="Show your keys"
                        className={`top-0 flex w-full items-center justify-center gap-2 rounded-lg border bg-card py-3 text-xs text-muted-foreground transition-opacity duration-200 hover:text-foreground md:absolute md:w-10 md:flex-col ${open ? "pointer-events-none opacity-0 max-md:hidden" : "opacity-100 delay-150"}`}>
                  <PanelLeftOpen className="size-4" /><span className="md:[writing-mode:vertical-rl]">Your keys</span>
                </button>
              </aside>
            )}
            <main className="min-w-0">{picked ? <ErrorBoundary key={picked.url}><Review url={picked.url} onReady={() => setKeysOpen(false)} /></ErrorBoundary> : (
              <div className="grid h-48 place-items-center rounded-xl border border-dashed text-muted-foreground">
                Pick a key on the left.
              </div>
            )}</main>
          </div>
        )}
      </div>
    </TooltipProvider>
  )
}
