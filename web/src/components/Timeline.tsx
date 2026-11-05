import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react"
import { ChartNoAxesCombined, X } from "lucide-react"
import * as echarts from "echarts/core"
import { CustomChart, LineChart, ScatterChart } from "echarts/charts"
import { BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent } from "echarts/components"
import { CanvasRenderer } from "echarts/renderers"
import type { Analysis } from "@/lib/analysis"
import { pullShort } from "@/lib/review"
import { fmtClock, reveal } from "@/lib/timeline"
import { CLASS_COLORS, lsGet, lsSet } from "@/lib/wow"
import { deathId } from "@/components/Deaths"
import { cn } from "@/lib/utils"

echarts.use([LineChart, ScatterChart, CustomChart, GridComponent, TooltipComponent, DataZoomComponent, BrushComponent,
             MarkAreaComponent, MarkLineComponent, CanvasRenderer])

type Marker = { t_s: number; kind: string; label: string; tip?: string[]; icon?: string; severity?: number; index?: number
                covered?: boolean; avoid?: string | null; kick?: boolean; risky?: boolean }
type Span = { lane: number | "group"; name: string; scope: "personal" | "external" | "group"; start_s: number; end_s: number
              protective: boolean; icon?: string; minor: boolean; tip: string[]; covered?: string[] }
type Use = { lane: number; t_s: number; kind: string; name: string; icon?: string; tip: string[] }
type Lane = { id: number; name: string; class: string; role: string }
export type TPull = { label: string; boss?: boolean; start_s: number; end_s: number; phases?: { name: string; start_s: number; end_s: number }[] }
export type TimelineData = {
  bin_s: number; group: (number | null)[]; lowest: (number | null)[]; pulls: TPull[]; markers?: Marker[]; lanes?: Lane[]
  defense?: Span[]; uses?: Use[]; intake?: number[]; intake_frac?: number[]; avoid_hits?: [number, string, number][]
  casts?: number[]; uncovered_hits?: number[]
}

const EVENT_LANES: [string, string][] = [["cooldown", "Healing cooldowns"], ["personal", "Personal"], ["trinket", "Your trinkets"],
                                         ["hit", "Big group hits"], ["death", "Deaths"], ["finding", "Findings"]]
const GAP = 4             // seconds of empty space drawn between pulls (the real gaps are squeezed out)
const LANE_H = 24, HP_H = 170, LEFT = 118, RIGHT = 14
const iconUrl = (icon: string) => `https://wow.zamimg.com/images/wow/icons/medium/${encodeURIComponent(icon)}.jpg`
const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim()
const pct = (v?: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`)

// Run time <-> chart position: pulls side by side, gaps between them squeezed to GAP seconds.
function squeeze(pulls: TPull[]) {
  const offs: number[] = []
  let c = 0
  for (const p of pulls) { offs.push(c - p.start_s); c += p.end_s - p.start_s + GAP }
  const toC = (s: number) => {
    let i = pulls.findIndex((p) => s <= p.end_s)
    if (i < 0) i = pulls.length - 1
    return Math.max(pulls[i].start_s, Math.min(s, pulls[i].end_s)) + offs[i]
  }
  const fromC = (x: number) => {
    for (let i = pulls.length - 1; i >= 0; i--) if (x >= pulls[i].start_s + offs[i]) return Math.min(x - offs[i], pulls[i].end_s)
    return pulls[0]?.start_s ?? 0
  }
  return { toC, fromC, total: Math.max(1, c - GAP), pullAt: (s: number) => pulls.findIndex((p) => s >= p.start_s && s <= p.end_s) }
}

// Group defensives are drawn once on a "Group" row, not on every member they covered.
function groupDefenses(defense: Span[], lanes: Lane[]): Span[] {
  const name = (id: Span["lane"]) => lanes.find((m) => m.id === id)?.name || "pet"
  const out: Span[] = []
  for (const d of defense.filter((x) => x.scope === "group").sort((a, b) => a.start_s - b.start_s)) {
    const same = out.find((o) => o.name === d.name && Math.abs(o.start_s - d.start_s) <= 2)
    if (same) { same.end_s = Math.max(same.end_s, d.end_s); same.covered!.push(name(d.lane)); continue }
    out.push({ ...d, lane: "group", covered: [name(d.lane)] })
  }
  for (const o of out) {
    o.tip = [o.tip[0].replace(/ on [^ ]+( from)?/, (_, from) => (from ? " from" : "")), o.tip[1],
             o.covered!.length >= lanes.length ? "Covered the whole group" : `Covered ${o.covered!.length} of ${lanes.length}: ${o.covered!.join(", ")}`]
  }
  return out
}

const tipHtml = (lines: string[]) => {
  const esc = (s: string) => s.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]!)
  return `<b>${esc(lines[0] || "")}</b>${lines.slice(1).map((l) => `<br>${esc(l)}`).join("")}`
}

export type TimelineHandle = { focus: (s: number, e?: number | null) => void }

// Whole run on one chart: HP on top, then your events, then each player's defensives. The slider underneath is the
// overview (drag its ends to zoom); Ctrl+wheel zooms; drag across the chart to analyze a range.
export const Timeline = forwardRef<TimelineHandle, { res: Analysis }>(function Timeline({ res }, ref) {
  const t = res.timeline as TimelineData
  const box = useRef<HTMLDivElement>(null), section = useRef<HTMLElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  const sc = useMemo(() => squeeze(t.pulls), [t])
  const [view, setView] = useState<[number, number]>([0, sc.total])
  const [mark, setMark] = useState<[number, number] | null>(null)
  const [sel, setSel] = useState<[number, number] | null>(null)
  const [party, setParty] = useState(lsGet("tl2:party") !== "0")
  const [minor, setMinor] = useState(lsGet("tl2:minor") === "1")
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"))
  const [readout, setReadout] = useState("")

  useEffect(() => {  // re-colour the chart when the theme changes
    const o = new MutationObserver(() => setDark(document.documentElement.classList.contains("dark")))
    o.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] })
    return () => o.disconnect()
  }, [])

  const lanes = t.lanes || []
  const groupSpans = useMemo(() => groupDefenses(t.defense || [], lanes), [t])  // eslint-disable-line react-hooks/exhaustive-deps
  const partyRows = party ? [...(groupSpans.length ? [{ id: "group" as const, name: "Group", class: "" }] : []), ...lanes] : []
  const eventsTop = 28 + HP_H + 18, partyTop = eventsTop + EVENT_LANES.length * LANE_H + 14
  const plotBottom = party ? partyTop + partyRows.length * LANE_H : eventsTop + EVENT_LANES.length * LANE_H
  const height = plotBottom + 30 + 34

  const zoomTo = (c0: number, c1: number) => chart.current?.dispatchAction({ type: "dataZoom", startValue: c0, endValue: c1 })
  const focus = (s: number, e?: number | null) => {
    const end = e ?? s, pad = e != null ? Math.max(3, (end - s) * 0.15) : 15
    const i = Math.max(0, sc.pullAt(s))
    const p = t.pulls[i] || t.pulls[0]
    zoomTo(sc.toC(Math.max(p.start_s, s - pad)), sc.toC(Math.min(p.end_s, end + pad)))
    setMark([s, end])
    section.current?.scrollIntoView({ behavior: "smooth", block: "start" })
  }
  useImperativeHandle(ref, () => ({ focus }))

  // Data that only changes with the run (and the party toggles).
  const data = useMemo(() => {
    const line = (arr: (number | null)[]) => {
      const out: [number, number | null][] = []
      arr.forEach((v, k) => {
        const s = k * t.bin_s
        if (sc.pullAt(s) >= 0) out.push([sc.toC(s), v])
        else if (out.length && out[out.length - 1][1] != null) out.push([sc.toC(s), null])  // break the line between pulls
      })
      return out
    }
    const rowOf = (id: Span["lane"]) => partyRows.findIndex((r) => r.id === id)
    const spans = [...groupSpans, ...(t.defense || []).filter((d) => d.scope !== "group")]
      .filter((d) => (minor || !d.minor) && rowOf(d.lane) >= 0 && sc.pullAt(d.start_s) >= 0)
    return { group: line(t.group), lowest: line(t.lowest), spans, rowOf }
  }, [t, sc, party, minor, groupSpans])  // eslint-disable-line react-hooks/exhaustive-deps

  const option = useMemo(() => {
    void dark
    const C = { text: css("--foreground"), muted: css("--muted-foreground"), grid: css("--border"), card: css("--card"),
                primary: css("--primary"), good: css("--good"), warn: css("--warn"), bad: css("--bad"), high: css("--high"), low: css("--low") }
    const span = view[1] - view[0]
    const plotW = Math.max(200, (box.current?.clientWidth || 900) - LEFT - RIGHT)
    const fits = (c0: number, c1: number, text: string) => (c1 - c0) / span * plotW > text.length * 6.5 + 8
    const dense = span > 240  // zoomed out: plain marks instead of spell icons, which would pile up
    const visible = (s: number) => sc.pullAt(s) >= 0
    const shade = (a: number, b: number, color: string, name = "", bold = false) => [
      { xAxis: sc.toC(a), itemStyle: { color }, name, label: { show: !!name && fits(sc.toC(a), sc.toC(b), name),
        color: bold ? C.text : C.muted, fontWeight: bold ? 700 : 400, fontSize: 11, position: "insideTopLeft" as const, distance: [2, -14] } },
      { xAxis: sc.toC(b) }]
    const areas: ReturnType<typeof shade>[] = []
    t.pulls.forEach((p) => {
      areas.push(shade(p.start_s, p.end_s, p.boss ? `${C.primary}1a` : `${C.muted}0d`, pullShort(p.label).split(" (")[0], !!p.boss))
      ;(p.phases || []).forEach((ph, i) => { if (i % 2 === 1) areas.push(shade(ph.start_s, Math.min(ph.end_s, p.end_s), `${C.primary}14`, ph.name)) })
    })
    ;(t.uncovered_hits || []).forEach((s) => areas.push(shade(s - 1, s + 1, `${C.bad}40`)))
    if (mark) areas.push(shade(mark[0] - 1, mark[1] + 1, `${C.primary}30`))

    const ev = (t.markers || []).filter((m) => visible(m.t_s) && EVENT_LANES.some(([k]) => k === m.kind))
    const evRow = (k: string) => EVENT_LANES.findIndex(([x]) => x === k)
    const evItem = (m: Marker) => {
      const base = { value: [sc.toC(m.t_s), evRow(m.kind)], tip: m.tip || [m.label], m }
      if (m.kind === "death") return { ...base, symbol: "path://M0,0L10,10M10,0L0,10", symbolSize: 11, itemStyle: { color: "none", borderColor: C.bad, borderWidth: 2.5 } }
      if (m.kind === "finding") {
        const col = (m.severity || 0) >= 70 ? C.bad : (m.severity || 0) >= 45 ? C.warn : C.low
        return { ...base, symbol: "circle", symbolSize: 11, itemStyle: { color: col, borderColor: C.card, borderWidth: 2 } }
      }
      if (m.kind === "hit" && m.kick) return { ...base, symbol: "diamond", symbolSize: 11, itemStyle: { color: C.warn } }
      if (m.kind === "hit") return { ...base, symbol: "roundRect", symbolSize: [3, 14], itemStyle: { color: m.covered === false ? C.bad : C.muted } }
      if (m.icon && !dense) return { ...base, symbol: `image://${iconUrl(m.icon)}`, symbolSize: LANE_H - 6 }
      return { ...base, symbol: "roundRect", symbolSize: [7, 13], itemStyle: { color: C.text, opacity: 0.75 } }
    }
    const scopeColor = { personal: C.primary, external: C.high, group: C.good }
    const xAxis = [0, 1, 2].map((i) => ({
      type: "value" as const, gridIndex: i, min: 0, max: sc.total, splitLine: { show: false }, axisTick: { show: false },
      axisLine: { show: false }, axisLabel: { show: i === (party ? 2 : 1), color: C.muted, formatter: (v: number) => fmtClock(sc.fromC(v)), hideOverlap: true },
      axisPointer: { show: true, snap: false, lineStyle: { color: C.muted, opacity: 0.5 }, label: { show: false } },
    }))
    const laneAxis = (gridIndex: number, rows: string[], color?: (i: number) => string) => ({
      type: "category" as const, gridIndex, data: rows, inverse: true, axisTick: { show: false }, axisLine: { show: false },
      splitLine: { show: true, lineStyle: { color: C.grid, opacity: 0.6 } }, boundaryGap: true,
      axisLabel: { color: color ? (_: string, i: number) => color(i) : C.muted, fontSize: 12, fontWeight: 500, width: LEFT - 16, overflow: "truncate" as const },
    })
    return {
      animation: false,
      textStyle: { fontFamily: "inherit" },
      grid: [
        { left: LEFT, right: RIGHT, top: 28, height: HP_H },
        { left: LEFT, right: RIGHT, top: eventsTop, height: EVENT_LANES.length * LANE_H },
        { left: LEFT, right: RIGHT, top: partyTop, height: Math.max(1, partyRows.length) * LANE_H, show: false },
      ],
      xAxis,
      yAxis: [
        { type: "value" as const, gridIndex: 0, min: 0, max: 1, interval: 0.5, axisLabel: { color: C.muted, formatter: (v: number) => `${v * 100}%` },
          splitLine: { lineStyle: { color: C.grid } }, name: "HP", nameLocation: "middle" as const, nameGap: 60, nameTextStyle: { color: C.muted, fontWeight: 600 } },
        laneAxis(1, EVENT_LANES.map(([, l]) => l)),
        laneAxis(2, partyRows.map((r) => r.name), (i) => (partyRows[i]?.id === "group" ? C.text : CLASS_COLORS[partyRows[i]?.class] || C.muted)),
      ],
      axisPointer: { link: [{ xAxisIndex: "all" }], triggerTooltip: false },
      tooltip: { trigger: "item" as const, confine: true, backgroundColor: C.card, borderColor: C.grid, textStyle: { color: C.text, fontSize: 12 },
                 formatter: (p: { data: { tip?: string[] } }) => (p.data?.tip ? tipHtml(p.data.tip) : "") },
      dataZoom: [
        { type: "inside" as const, xAxisIndex: [0, 1, 2], zoomOnMouseWheel: "ctrl" as const, moveOnMouseWheel: false, moveOnMouseMove: false, minValueSpan: 5 },
        { type: "slider" as const, xAxisIndex: [0, 1, 2], top: plotBottom + 30, height: 26, left: LEFT, right: RIGHT, minValueSpan: 5,
          labelFormatter: (v: number) => fmtClock(sc.fromC(v)), textStyle: { color: C.muted }, borderColor: C.grid,
          fillerColor: `${C.primary}22`, handleStyle: { color: C.primary, borderColor: C.primary }, moveHandleStyle: { color: C.primary },
          dataBackground: { lineStyle: { color: C.muted }, areaStyle: { color: `${C.muted}33` } },
          selectedDataBackground: { lineStyle: { color: C.primary }, areaStyle: { color: `${C.primary}33` } } },
      ],
      brush: { xAxisIndex: [0, 1, 2], brushType: "lineX" as const, brushMode: "single" as const, transformable: false,
               brushStyle: { color: `${C.primary}22`, borderColor: C.primary, borderWidth: 1 }, outOfBrush: { colorAlpha: 1 } },
      series: [
        { name: "Group HP", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.group, showSymbol: false, sampling: "lttb" as const,
          lineStyle: { width: 2, color: C.primary }, itemStyle: { color: C.primary }, silent: true, z: 3,
          markArea: { silent: true, data: areas },
          markLine: { silent: true, symbol: "none", label: { show: false },
                      data: (t.markers || []).filter((m) => m.kind === "chain" && visible(m.t_s))
                        .map((m) => ({ xAxis: sc.toC(m.t_s), lineStyle: { color: m.risky ? C.warn : C.muted, type: "dashed" as const, width: 1.5 } })) } },
        { name: "Lowest player", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.lowest, showSymbol: false, sampling: "lttb" as const,
          lineStyle: { width: 1.5, color: C.high }, itemStyle: { color: C.high }, silent: true, z: 2 },
        { name: "Events", type: "scatter" as const, xAxisIndex: 1, yAxisIndex: 1, data: ev.map(evItem), z: 4,
          emphasis: { scale: 1.3 } },
        ...(party ? [
          { name: "Defensives", type: "custom" as const, xAxisIndex: 2, yAxisIndex: 2, z: 3,
            data: data.spans.map((d) => ({ value: [sc.toC(d.start_s), sc.toC(d.end_s), data.rowOf(d.lane)], tip: d.tip, d })),
            encode: { x: [0, 1], y: 2 },
            renderItem: (params: unknown, api: { value: (i: number) => number; coord: (v: number[]) => number[]; size: (v: number[]) => number[] }) => {
              const d = data.spans[(params as { dataIndex: number }).dataIndex]
              const [x0, y] = api.coord([api.value(0), api.value(2)]), [x1] = api.coord([api.value(1), api.value(2)])
              const h = api.size([0, 1])[1] * 0.62, w = Math.max(3, x1 - x0)
              const children: object[] = [{ type: "rect", shape: { x: x0, y: y - h / 2, width: w, height: h, r: 3 },
                                            style: { fill: scopeColor[d.scope], opacity: d.minor ? 0.3 : d.protective ? 0.6 : 0.4 } }]
              if (d.icon && !dense && w >= h) children.push({ type: "image", style: { image: iconUrl(d.icon), x: x0, y: y - h / 2, width: h, height: h } })
              return { type: "group", children }
            } },
          { name: "Potions & trinkets", type: "scatter" as const, xAxisIndex: 2, yAxisIndex: 2, z: 5,
            data: (t.uses || []).filter((u) => visible(u.t_s) && data.rowOf(u.lane) >= 0).map((u) => ({
              value: [sc.toC(u.t_s), data.rowOf(u.lane)], tip: u.tip,
              ...(u.icon && !dense ? { symbol: `image://${iconUrl(u.icon)}`, symbolSize: LANE_H - 8 }
                                   : { symbol: "circle", symbolSize: 8, itemStyle: { color: C.text, borderColor: C.card, borderWidth: 1.5 } }) })) },
        ] : []),
      ],
    }
  }, [t, sc, data, view, mark, party, dark, eventsTop, partyTop, plotBottom, partyRows])

  useEffect(() => {
    const c = echarts.init(box.current!)
    chart.current = c
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(box.current!)
    c.on("datazoom", () => {
      const dz = (c.getOption() as { dataZoom: { startValue: number; endValue: number }[] }).dataZoom[0]
      setView([dz.startValue, dz.endValue])
    })
    c.on("updateAxisPointer", (e: unknown) => {
      const info = (e as { axesInfo?: { axisDim: string; value: number }[] }).axesInfo?.find((a) => a.axisDim === "x")
      if (!info) return
      const s = sc.fromC(info.value), b = Math.round(s / t.bin_s), p = t.pulls[sc.pullAt(s)]
      const ph = p?.phases?.find((x) => s >= x.start_s && s < x.end_s)
      setReadout(p ? `${fmtClock(s)} · ${pullShort(p.label).split(" (")[0]}${ph && ph.name !== "Start" ? ` · ${ph.name}` : ""} · Group ${pct(t.group[b])} · Lowest ${pct(t.lowest[b])}` : "")
    })
    c.on("brushEnd", (e: unknown) => {
      const r = (e as { areas: { coordRange: number[] }[] }).areas[0]?.coordRange
      if (r && Math.abs(r[1] - r[0]) >= 1) setSel([sc.fromC(r[0]), sc.fromC(r[1])])
    })
    c.on("click", (p: unknown) => {
      const m = (p as { data?: { m?: Marker } }).data?.m
      if (m?.kind === "finding" && m.index != null) reveal(`finding-${m.index}`)
      if (m?.kind === "death") reveal(deathId(fmtClock(Math.floor(m.t_s)), m.label.replace(/ died$/, "")))
    })
    return () => { ro.disconnect(); c.dispose(); chart.current = null }
  }, [sc, t])

  useEffect(() => {
    const c = chart.current
    if (!c) return
    c.setOption(option, { replaceMerge: ["series"] })
    c.resize()
    // Dragging across the chart selects a range (zoom with the slider or Ctrl+wheel).
    c.dispatchAction({ type: "takeGlobalCursor", key: "brush", brushOption: { brushType: "lineX", brushMode: "single" } })
  }, [option])

  const pullIdx = t.pulls.findIndex((p) => Math.abs(sc.toC(p.start_s) - view[0]) < 1 && Math.abs(sc.toC(p.end_s) - view[1]) < 1)
  const zoomed = view[1] - view[0] < sc.total - 1
  const toggle = (k: string, v: boolean, set: (v: boolean) => void) => { lsSet(k, v ? "1" : "0"); set(v) }
  const closeSel = () => { setSel(null); chart.current?.dispatchAction({ type: "brush", areas: [] }) }

  return (
    <section ref={section} className="flex scroll-mt-4 flex-col gap-3 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid size-8 place-items-center rounded-lg bg-primary/12 text-primary"><ChartNoAxesCombined className="size-4" /></span>
        <h2 className="text-lg font-semibold">Timeline</h2>
        <select className="h-8 rounded-md border bg-background px-2 text-sm" aria-label="Pull" value={pullIdx}
                onChange={(e) => { const i = +e.target.value; setMark(null); if (i < 0) zoomTo(0, sc.total); else zoomTo(sc.toC(t.pulls[i].start_s), sc.toC(t.pulls[i].end_s)) }}>
          <option value={-1}>{zoomed && pullIdx < 0 ? "Zoomed" : "Whole run"}</option>
          {t.pulls.map((p, i) => <option key={i} value={i}>{p.label} ({fmtClock(p.start_s)})</option>)}
        </select>
        {zoomed && <button className="text-sm text-primary hover:underline" onClick={() => { setMark(null); zoomTo(0, sc.total) }}>Show whole run</button>}
        <div className="ml-auto flex items-center gap-4 text-sm">
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={party} onChange={(e) => toggle("tl2:party", e.target.checked, setParty)} />Party lanes</label>
          {party && <label className="flex items-center gap-1.5"><input type="checkbox" checked={minor} onChange={(e) => toggle("tl2:minor", e.target.checked, setMinor)} />Minor defensives</label>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><i className="h-0.5 w-4 bg-primary" />Group HP</span>
        <span className="flex items-center gap-1.5"><i className="h-0.5 w-4 bg-high" />Lowest player</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-1 rounded-sm bg-bad" />Big hit, unanswered</span>
        {party && <>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-primary/60" />Personal</span>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-high/60" />External</span>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-good/60" />Group</span>
        </>}
        <span className="ml-auto tabular-nums text-foreground">{readout || "Drag across the chart to analyze a range · Ctrl+wheel or the slider to zoom"}</span>
      </div>
      <div ref={box} style={{ height }} className="w-full" />
      {sel && <Selection t={t} sel={sel} onClose={closeSel} onZoom={() => zoomTo(sc.toC(sel[0]), sc.toC(sel[1]))} />}
    </section>
  )
})

// "Analyze selection": a summary of just the dragged range, from the timeline data already loaded.
function Selection({ t, sel: [a, b], onClose, onZoom }: { t: TimelineData; sel: [number, number]; onClose: () => void; onZoom: () => void }) {
  const b0 = Math.max(0, Math.floor(a / t.bin_s)), b1 = Math.min(t.group.length - 1, Math.ceil(b / t.bin_s))
  const inPull = (s: number) => t.pulls.some((q) => s >= q.start_s && s <= q.end_s)
  const bins: number[] = []
  for (let k = b0; k <= b1; k++) if (inPull(k * t.bin_s)) bins.push(k)
  const pullsIn = t.pulls.filter((q) => q.end_s >= a && q.start_s <= b)
  const fight = pullsIn.reduce((s, q) => s + Math.max(0, Math.min(b, q.end_s) - Math.max(a, q.start_s)), 0)
  const secs = Math.max(1, fight)
  const vals = bins.map((k) => t.group[k]).filter((x): x is number => x != null)
  const avg = vals.length ? vals.reduce((s, x) => s + x, 0) / vals.length : null
  const lowest = Math.min(...bins.map((k) => t.lowest[k] ?? 1))
  const intake = bins.reduce((s, k) => s + (t.intake?.[k] || 0), 0)
  const inFrac = bins.reduce((s, k) => s + (t.intake_frac?.[k] || 0), 0) / secs
  const avoidBy: Record<string, number> = {}
  for (const [k, n, amt] of t.avoid_hits || []) if (k >= b0 && k <= b1) avoidBy[n] = (avoidBy[n] || 0) + amt
  const avoid = Object.values(avoidBy).reduce((s, x) => s + x, 0)
  const avoidNames = Object.entries(avoidBy).sort((x, y) => y[1] - x[1]).slice(0, 3).map(([n]) => n)
  const casts = bins.reduce((s, k) => s + (t.casts?.[k] || 0), 0)
  const inside = (s: number) => s >= a && s <= b
  const marks = (kinds: string[]) => (t.markers || []).filter((m) => kinds.includes(m.kind) && inside(m.t_s))
  const list = <T,>(xs: T[], f: (x: T) => string) => (xs.length ? xs.map(f).join(", ") : "none")
  const defs: Record<string, number> = {}
  for (const d of (t.defense || []).filter((d) => inside(d.start_s))) defs[d.name] = (defs[d.name] || 0) + 1
  const groupDefs = groupDefenses((t.defense || []).filter((d) => inside(d.start_s)), t.lanes || []).length
  const name = (id: number) => t.lanes?.find((l) => l.id === id)?.name || "?"
  const findings = marks(["finding"])
  const Row = ({ k, children }: { k: string; children: React.ReactNode }) => <li><b>{k}:</b> {children}</li>
  return (
    <div className="rounded-lg border bg-secondary p-4 text-sm">
      <div className="mb-2 flex items-center gap-3">
        <b>Selection {fmtClock(a)}-{fmtClock(b)}</b>
        <span className="text-muted-foreground">{pullsIn.length > 1 ? `${pullsIn.length} pulls, ${Math.round(fight)}s in combat (gaps between pulls left out)` : `${Math.round(secs)}s`}</span>
        <button className="ml-auto text-primary hover:underline" onClick={onZoom}>Zoom to it</button>
        <button className="rounded p-1 hover:bg-accent" aria-label="Close selection" onClick={onClose}><X className="size-4" /></button>
      </div>
      <ul className="flex flex-col gap-1">
        <Row k="Group HP">{pct(avg)} average, lowest player {pct(lowest)}</Row>
        <Row k="Damage taken">{(inFrac * 100).toFixed(1)}% of group HP per second
          {intake && avoid ? ` · ${pct(avoid / intake)} avoidable (${avoidNames.join(", ")})` : " · nothing tagged avoidable"}</Row>
        <Row k="Your casts">{casts} ({Math.round(casts / secs * 60)} per minute) · cooldowns:{" "}
          {list(marks(["cooldown", "personal", "trinket"]), (m) => `${m.label} ${fmtClock(m.t_s)}`)}</Row>
        <Row k="Defensives pressed">{Object.keys(defs).length ? Object.entries(defs).map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(", ") : "none"}
          {groupDefs ? ` (${groupDefs} group-wide)` : ""}</Row>
        <Row k="Potions / healthstones / trinkets">{list((t.uses || []).filter((u) => inside(u.t_s)), (u) => `${u.name} (${name(u.lane)}) ${fmtClock(u.t_s)}`)}</Row>
        <Row k="Deaths">{list(marks(["death"]), (m) => `${m.label} ${fmtClock(m.t_s)}`)}</Row>
        <Row k="Findings">{findings.length ? findings.map((m, i) => (
          <span key={i}>{i > 0 && ", "}<button className={cn("text-primary hover:underline")} onClick={() => reveal(`finding-${m.index}`)}>{m.label}</button></span>
        )) : "none"}</Row>
      </ul>
    </div>
  )
}
