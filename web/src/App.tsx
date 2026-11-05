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
  const open = keysOpen || !picked

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
                  <KeyList selected={picked?.url} onPick={pick} version={version} />
                </div>
                <button inert={open} onClick={() => setKeysOpen(true)} title="Show your keys" aria-label="Show your keys"
                        className={`top-0 flex w-full items-center justify-center gap-2 rounded-lg border bg-card py-3 text-xs text-muted-foreground transition-opacity duration-200 hover:text-foreground md:absolute md:w-10 md:flex-col ${open ? "pointer-events-none opacity-0 max-md:hidden" : "opacity-100 delay-150"}`}>
                  <PanelLeftOpen className="size-4" /><span className="md:[writing-mode:vertical-rl]">Your keys</span>
                </button>
              </aside>
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
