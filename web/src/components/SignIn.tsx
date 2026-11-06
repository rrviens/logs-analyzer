import { useEffect, useRef, useState } from "react"
import { Check, CircleAlert } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"

type State = "idle" | "waiting" | "ok" | "closed" | "failed"
const TEXT: Record<Exclude<State, "idle">, [string, string]> = {
  waiting: ["Signing in with Warcraft Logs", "Finish in the window that opened. This page updates by itself."],
  ok: ["Signed in", "Loading your keys..."],
  closed: ["The window closed before sign-in finished",
           "If Warcraft Logs showed its home page after you logged in, try again - it goes straight through now."],
  failed: ["Sign-in didn't finish", "Warcraft Logs didn't complete it. Try again in a moment."],
}

// Why sign-in failed, from /callback's reason code (never a token or code).
const REASONS: Record<string, string> = {
  rate_limited: "Warcraft Logs is limiting requests right now (rate limit). Try again in a minute.",
  timeout: "Warcraft Logs took too long to answer (timeout). Try again in a minute.",
  state: "The sign-in expired or was started in another tab (state mismatch). Start it again from this page.",
  invalid_grant: "Warcraft Logs rejected the login code (invalid_grant) - it may have expired. Try again; if it keeps failing, the site's redirect URL may not match your Warcraft Logs API client settings.",
  redirect_mismatch: "The site's redirect URL doesn't match the Warcraft Logs API client settings (redirect mismatch). The site owner needs to fix REDIRECT_URI or the client's redirect URL.",
  invalid_client: "Warcraft Logs didn't accept this site's API client (invalid_client). The site owner needs to check the client id and secret.",
  cancelled: "Sign-in was cancelled on Warcraft Logs.",
}

// Sign-in runs in a popup (Warcraft Logs' pages can't be embedded); its last page posts back and closes itself.
export function SignIn() {
  const [state, setState] = useState<State>("idle")
  const [failed] = useState(() => {
    const q = new URLSearchParams(location.search), why = q.get("signin")
    if (!why) return ""
    history.replaceState(null, "", "/")  // a reload shouldn't report it again
    const reason = q.get("reason") || why
    return REASONS[reason] || `Warcraft Logs didn't complete the login (${reason}). Try again in a minute; if it keeps failing, the site's redirect URL may not match your Warcraft Logs API client settings.`
  })
  const pop = useRef<Window | null>(null)
  const done = useRef(false)

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== location.origin || e.data?.type !== "hr-signin") return
      done.current = true
      if (e.data.ok) {
        setState("ok")
        setTimeout(() => location.reload(), 400)
      } else setState("failed")
    }
    window.addEventListener("message", onMessage)
    const poll = setInterval(() => {
      if (pop.current?.closed && !done.current && state === "waiting") setState("closed")
    }, 500)
    return () => { window.removeEventListener("message", onMessage); clearInterval(poll) }
  }, [state])

  const start = () => {
    const w = 520, h = 720
    const x = Math.max(0, screenX + (outerWidth - w) / 2), y = Math.max(0, screenY + (outerHeight - h) / 2)
    pop.current = window.open("/login?popup=true", "hr-signin", `popup=yes,width=${w},height=${h},left=${x},top=${y}`)
    if (!pop.current) { location.href = "/login"; return }  // popups blocked: the full-page way
    done.current = false
    setState("waiting")
  }

  return (
    <main className="grid flex-1 place-items-center p-6">
      <div className="max-w-md rounded-2xl border bg-card p-8 shadow-sm">
        <h1 className="mb-2 text-2xl font-bold">Healer Review</h1>
        <p className="mb-3 text-muted-foreground">Context-aware Mythic+ healing review, compared with top players of your spec at your key level.</p>
        <p className="mb-6 text-sm text-muted-foreground">Sign in with your Warcraft Logs account: your analyses run on your own account and API
          limit, and your private logs stay visible only to you.</p>
        <Button size="lg" onClick={start}>Sign in with Warcraft Logs</Button>
        {failed && <p className="mt-4 flex gap-2 text-sm text-bad" role="alert"><CircleAlert className="mt-0.5 size-4 shrink-0" />Sign-in failed: {failed}</p>}
      </div>
      <Dialog open={state !== "idle"} onOpenChange={(o) => { if (!o) { pop.current?.close(); done.current = true; setState("idle") } }}>
        <DialogContent className="text-center sm:max-w-sm">
          {state !== "idle" && (
            <>
              <div className="mx-auto grid size-9 place-items-center">
                {state === "waiting" ? <span className="size-6 animate-spin rounded-full border-3 border-border border-t-primary" />
                  : state === "ok" ? <Check className="size-7 text-good" /> : <CircleAlert className="size-7 text-bad" />}
              </div>
              <DialogTitle>{TEXT[state][0]}</DialogTitle>
              <DialogDescription>{TEXT[state][1]}</DialogDescription>
              <div className="flex justify-center gap-2">
                {state === "waiting" && <Button variant="outline" onClick={() => pop.current?.focus()}>Show window</Button>}
                {(state === "closed" || state === "failed") && <Button onClick={start}>Try again</Button>}
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </main>
  )
}
