import { ExternalLink } from "lucide-react"
import { useAnalysis } from "@/lib/analysis"
import { Group } from "@/components/Group"
import { RunHeader } from "@/components/RunHeader"
import { buttonVariants } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

// One key's review. Header and group are here; the remaining sections still open in the classic view.
export function Review({ url }: { url: string }) {
  const a = useAnalysis(url)
  const shown = a.result || a.preview
  if (a.error && !shown) {
    return (
      <div className="rounded-xl border bg-card p-6">
        <h2 className="mb-1 text-lg font-semibold">Couldn't analyze this run</h2>
        <p className="mb-4 text-sm text-muted-foreground">{a.error}</p>
        <button className={buttonVariants({ variant: "outline", size: "sm" })} onClick={a.reanalyze}>Try again</button>
      </div>
    )
  }
  if (!shown) {
    return (
      <div className="flex flex-col gap-3 rounded-xl border bg-card p-6">
        <h2 className="text-lg font-semibold">Analyzing</h2>
        {a.progress.map((p, i) => <p key={i} className="text-sm text-muted-foreground">{p}</p>)}
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  const busy = !a.result || !!a.updating
  return (
    <div className="flex flex-col gap-5">
      {(a.updating || (!a.result && a.progress.length > 0)) && (
        <p className="rounded-lg border bg-secondary px-3 py-2 text-sm text-muted-foreground">
          {a.updating || `Your run is ready; comparing with peers... ${a.progress.at(-1) || ""}`}
        </p>
      )}
      <section className="flex flex-col gap-5 rounded-xl border bg-card p-5">
        <RunHeader res={shown} url={url} onReanalyze={a.reanalyze} busy={busy} />
        <Group res={shown} />
      </section>
      <a className={buttonVariants({ variant: "secondary" })} href={`/?run=${encodeURIComponent(url)}`}>
        Findings, death recaps and timeline (classic view)<ExternalLink />
      </a>
    </div>
  )
}
