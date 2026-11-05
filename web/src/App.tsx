import { useEffect, useState } from "react"
import { PanelLeftOpen } from "lucide-react"
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
  const [keysOpen, setKeysOpen] = useState(true)  // stays open; "Hide your keys" folds it to a rail by hand
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
        <Header me={me} settings={settings} theme={theme} setTheme={setTheme} current={picked?.dungeon && `${picked.dungeon} +${picked.level}`}
                onCharacter={() => { setPicked(null); setVersion((v) => v + 1) }} />
        {!signedIn ? (shared && linked ? (
          <div className="mx-auto flex w-full max-w-[90rem] flex-col gap-6 px-5 py-6 md:px-10">
            <p className="flex flex-wrap items-center gap-3 rounded-lg border bg-secondary px-4 py-2.5 text-sm text-muted-foreground">
              Viewing a saved analysis of a public run. Sign in to analyze new runs or your private logs.
              <a href="/login" className="ml-auto font-medium text-primary hover:underline">Sign in with Warcraft Logs</a>
            </p>
            <ErrorBoundary><Review url={linked} saved={shared} /></ErrorBoundary>
          </div>
        ) : <SignIn />) : (
          <div className={`grid flex-1 transition-[grid-template-columns] duration-300 ease-out motion-reduce:transition-none ${open ? "md:grid-cols-[18.5rem_1fr]" : "md:grid-cols-[3rem_1fr]"}`}>
            {!settings.name ? <p className="p-5 text-sm text-muted-foreground">Pick your character at the top right to see your keys.</p> : (
              // Both stay mounted: the column width animates while the list fades into the rail (and back).
              <aside className="relative min-w-0 border-b bg-sidebar md:sticky md:top-14 md:h-[calc(100svh-3.5rem)] md:overflow-x-clip md:border-r md:border-b-0">
                <div inert={!open} className={`flex h-full w-full flex-col transition-opacity duration-200 md:w-[18.5rem] ${open ? "opacity-100 delay-100" : "opacity-0 max-md:hidden"}`}>
                  <form className="flex gap-2 border-b p-3" onSubmit={(e) => {
                    e.preventDefault()
                    const v = new FormData(e.currentTarget).get("url")?.toString().trim()
                    if (v) setPicked({ url: v } as Run)
                  }}>
                    <Input name="url" placeholder="Paste a Warcraft Logs URL" autoComplete="off" className="h-8 text-sm" />
                    <Button type="submit" size="sm" variant="secondary">Analyze</Button>
                  </form>
                  <KeyList selected={picked?.url} onPick={setPicked} version={version} onHide={picked ? () => setKeysOpen(false) : undefined} />
                </div>
                <button inert={open} onClick={() => setKeysOpen(true)} title="Show your keys" aria-label="Show your keys"
                        className={`flex w-full items-center justify-center gap-2 py-3 text-xs text-muted-foreground transition-opacity duration-200 hover:bg-accent hover:text-foreground md:absolute md:inset-0 md:flex-col md:justify-start md:pt-4 ${open ? "pointer-events-none opacity-0 max-md:hidden" : "opacity-100 delay-150"}`}>
                  <PanelLeftOpen className="size-4" /><span className="md:[writing-mode:vertical-rl]">Your keys</span>
                </button>
              </aside>
            )}
            <main className="min-w-0 px-5 py-6 md:px-10 md:py-8"><div className="mx-auto max-w-[90rem]">
              {picked ? <ErrorBoundary key={picked.url}><Review url={picked.url} /></ErrorBoundary> : (
                <div className="py-16">
                  <h1 className="text-2xl font-semibold tracking-tight">Pick a key to review</h1>
                  <p className="mt-2 max-w-prose text-muted-foreground">Choose one of your keys on the left, or paste a Warcraft Logs URL to review any healer's run.</p>
                </div>
              )}
            </div></main>
          </div>
        )}
      </div>
    </TooltipProvider>
  )
}
