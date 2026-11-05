import { createContext, Fragment, useContext } from "react"

// Lets any text on the review point the timeline at a moment: time links, death and DPS-window clicks.
type Ctx = { focus: (s: number, e?: number | null) => void; spells?: { re: RegExp; ids: Record<string, number> } | null }
export const TimelineContext = createContext<Ctx>({ focus: () => {} })
export const useFocus = () => useContext(TimelineContext).focus

// Spell names known for this run (result.spell_ids), longest first, as one pattern.
export function spellPattern(ids?: Record<string, number> | null) {
  const names = Object.keys(ids || {}).sort((a, b) => b.length - a.length)
  if (!names.length) return null
  return { re: new RegExp(`(?<![\\w'])(${names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})(?![\\w'])`, "g"), ids: ids! }
}

// A spell name as a Wowhead link: the tooltip script shows the spell on hover; a plain click doesn't navigate.
function spellLinks(text: string, sp: Ctx["spells"], key: string): React.ReactNode[] {
  if (!sp) return [text]
  const out: React.ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(sp.re)) {
    out.push(text.slice(last, m.index))
    out.push(<a key={`${key}-${m.index}`} href={`https://www.wowhead.com/spell=${sp.ids[m[1]]}`} target="_blank" rel="noopener"
                className="underline decoration-muted-foreground/50 decoration-dotted underline-offset-2"
                onClick={(e) => { e.stopPropagation(); if (!e.ctrlKey && !e.metaKey) e.preventDefault() }}>{m[1]}</a>)
    last = m.index! + m[0].length
  }
  out.push(text.slice(last))
  return out
}

export const fmtClock = (s: number) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` }

// Every m:ss (or m:ss-m:ss) in a text becomes a link to that moment on the timeline.
const TIME = /(^|[^\d:.])(\d{1,2}):(\d{2})(?:(\s?[-–]\s?)(\d{1,2}):(\d{2}))?(?![\d:])/g
export function Tx({ children }: { children?: string | null }) {
  const { focus, spells } = useContext(TimelineContext)
  if (!children) return null
  const out: React.ReactNode[] = []
  let last = 0
  for (const m of children.matchAll(TIME)) {
    const [all, pre, m1, s1, sep, m2, s2] = m
    const at = m.index! + pre.length
    out.push(...spellLinks(children.slice(last, at), spells, `s${last}`))
    const a = +m1 * 60 + +s1, b = m2 != null ? +m2 * 60 + +s2 : null
    out.push(
      <button key={at} type="button" className="text-primary underline decoration-dotted underline-offset-2 hover:decoration-solid"
              onClick={(e) => { e.stopPropagation(); focus(a, b) }}>
        {`${m1}:${s1}` + (m2 != null ? `${sep}${m2}:${s2}` : "")}
      </button>,
    )
    last = m.index! + all.length
  }
  out.push(...spellLinks(children.slice(last), spells, `s${last}`))
  return <>{out.map((x, i) => <Fragment key={i}>{x}</Fragment>)}</>
}

// Scroll to a card (finding / death recap) and open it if it's closed.
export function reveal(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  const trigger = el.querySelector<HTMLElement>("[aria-expanded]")
  if (trigger?.getAttribute("aria-expanded") === "false") trigger.click()
  el.scrollIntoView({ behavior: "smooth", block: "center" })
  el.animate([{ outline: "2px solid var(--primary)" }, { outline: "2px solid transparent" }], { duration: 1600 })
}
