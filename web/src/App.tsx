import { useEffect, useState } from "react"
import { ExternalLink } from "lucide-react"
import { api, type Me, type Run, type Settings } from "@/lib/api"
import { useTheme } from "@/lib/theme"
import { RESULT_LABEL, result } from "@/lib/wow"
import { Header } from "@/components/Header"
import { KeyList } from "@/components/KeyList"
import { SignIn } from "@/components/SignIn"
import { buttonVariants } from "@/components/ui/button"
import { TooltipProvider } from "@/components/ui/tooltip"

export default function App() {
  const [theme, setTheme] = useTheme()
  const [me, setMe] = useState<Me | null>(null)
  const [settings, setSettings] = useState<Settings>({})
  const [version, setVersion] = useState(0)  // bumped when the character changes: everything reloads
  const [picked, setPicked] = useState<Run | null>(null)

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
          <div className="grid flex-1 gap-5 p-5 md:grid-cols-[20rem_1fr]">
            {settings.name ? <KeyList selected={picked?.url} onPick={setPicked} version={version} />
              : <p className="text-sm text-muted-foreground">Set your character (top right) to see your recent keys.</p>}
            <main>{picked ? <Picked run={picked} /> : (
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

// The review itself is still the classic page while it's being ported section by section.
function Picked({ run }: { run: Run }) {
  return (
    <div className="rounded-xl border bg-card p-6">
      <h2 className="text-2xl font-bold">{run.dungeon} +{run.level}</h2>
      <p className="mb-5 text-sm text-muted-foreground">{RESULT_LABEL[result(run)]} · {run.duration_min} min</p>
      <a className={buttonVariants()} href={`/?run=${encodeURIComponent(run.url)}`}>Open the review<ExternalLink className="size-4" /></a>
      <p className="mt-3 text-xs text-muted-foreground">The review opens in the classic view for now - its sections move here next.</p>
    </div>
  )
}
