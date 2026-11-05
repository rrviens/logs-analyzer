import { ChevronRight } from "lucide-react"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

// One moment of the run as a row: time with a coloured mark (what kind / how bad), what happened, and the fix on the
// right. Rows sit on hairlines, not in boxes; opening one indents its details under the title.
export function LogRow({ id, time, mark, title, fix, extra, children }: {
  id?: string; time: string; mark: string; title: React.ReactNode; fix?: string; extra?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <Collapsible id={id} className="scroll-mt-20 border-b first:border-t">
      <CollapsibleTrigger className="group grid w-full grid-cols-[4rem_1fr_auto] items-baseline gap-x-3 px-1 py-2.5 text-left hover:bg-accent/50">
        <span className="flex items-center gap-2 text-sm text-muted-foreground tabular-nums">
          <i className={cn("h-3.5 w-[3px] shrink-0 self-center rounded-full", mark)} />{time}
        </span>
        <span className="min-w-0">{title}</span>
        <span className="flex items-baseline gap-3 text-sm">
          {extra}
          {fix && <span className="hidden max-w-64 truncate font-medium sm:inline" title={fix}>{fix}</span>}
          <ChevronRight className="size-4 self-center text-muted-foreground transition-transform group-data-[panel-open]:rotate-90" />
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="mb-4 ml-[4.75rem] flex flex-col gap-2.5 border-l pr-1 pl-4 text-sm">
        {fix && <p className="font-medium sm:hidden">{fix}</p>}
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}
