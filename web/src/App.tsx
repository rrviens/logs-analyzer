import { useEffect, useState } from "react"
import { PanelLeftClose, PanelLeftOpen } from "lucide-react"
import { api, type Me, type Run, type Settings } from "@/lib/api"
import { useTheme } from "@/lib/theme"
import { Header } from "@/components/Header"
import { KeyList } from "@/components/KeyList"
import { SignIn } from "@/components/SignIn"
import { Review } from "@/components/Review"
import { TooltipProvider } from "@/components/ui/tooltip"

export default function App() {
  const [theme, setTheme] = useTheme()
  const [me, setMe] = useState<Me | null>(null)
  const [settings, setSettings] = useState<Settings>({})
  const [version, setVersion] = useState(0)  // bumped when the character changes: everything reloads
  const [picked, setPicked] = useState<Run | null>(null)
  const [keysOpen, setKeysOpen] = useState(true)  // folds to a rail while a review is open, so the review gets the width
  const pick = (r: Run) => { setPicked(r); setKeysOpen(false) }

  useEffect(() => { api<Me>("/api/me").then(setMe).catch(() => setMe({ mode: "local", user: null })) }, [])
  const signedIn = me && (me.mode === "local" || me.user)
  useEffect(() => { if (signedIn) api<Settings>("/api/settings").then(setSettings).catch(() => {}) }, [signedIn, version])

  if (!me) return null
  return (
    <TooltipProvider>
      <div className="flex min-h-svh flex-col">
        <Header me={me} settings={settings} theme={theme} setTheme={setTheme}
                onCharacter={() => { setPicked(null); setVersion((v) => v + 1) }} />
        {!signedIn ? <SignIn /> : (
          <div className={`mx-auto grid w-full max-w-[100rem] flex-1 gap-5 p-5 ${keysOpen || !picked ? "md:grid-cols-[20rem_1fr]" : "md:grid-cols-[2.5rem_1fr]"}`}>
            {!settings.name ? <p className="text-sm text-muted-foreground">Set your character (top right) to see your recent keys.</p>
              : keysOpen || !picked ? (
                <div className="flex min-w-0 flex-col gap-2">
                  {picked && (
                    <button className="flex items-center gap-1.5 self-end text-xs text-muted-foreground hover:text-foreground" onClick={() => setKeysOpen(false)}>
                      <PanelLeftClose className="size-4" />Hide keys
                    </button>
                  )}
                  <KeyList selected={picked?.url} onPick={pick} version={version} />
                </div>
              ) : (
                <button className="sticky top-5 flex h-fit flex-col items-center gap-2 rounded-lg border bg-card py-3 text-xs text-muted-foreground hover:text-foreground"
                        onClick={() => setKeysOpen(true)} title="Show your keys" aria-label="Show your keys">
                  <PanelLeftOpen className="size-4" /><span className="[writing-mode:vertical-rl]">Your keys</span>
                </button>
              )}
            <main className="min-w-0">{picked ? <Review key={picked.url} url={picked.url} /> : (
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
