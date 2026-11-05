import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react"
import { X } from "lucide-react"
import * as echarts from "echarts/core"
import { CustomChart, LineChart, ScatterChart } from "echarts/charts"
import { BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent } from "echarts/components"
import { CanvasRenderer } from "echarts/renderers"
import type { Analysis } from "@/lib/analysis"
import { pullShort } from "@/lib/review"
import { fmtClock, reveal, Tx } from "@/lib/timeline"
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
type Curve = [number, number, number, number][]  // [position in pull 0-1, p25, median, p75]
export type TPull = { label: string; boss?: boolean; start_s: number; end_s: number; phases?: { name: string; start_s: number; end_s: number }[]
                      deaths?: number; death_lines?: string[]; verdict?: string; avoid?: string; chain?: string
                      top_curve?: Curve | null; study_curve?: Curve | null; study_approx?: boolean }
export type TimelineData = {
  bin_s: number; group: (number | null)[]; lowest: (number | null)[]; mana?: (number | null)[]; pulls: TPull[]; markers?: Marker[]
  lanes?: Lane[]; defense?: Span[]; uses?: Use[]; intake?: number[]; intake_frac?: number[]; avoid_hits?: [number, string, number][]
  casts?: number[]; uncovered_hits?: number[]; lust?: { t_s: number; label: string }[]
  ready_lanes?: { name: string; ready: [number, number][]; ready_s: number }[]
}
type Base = "peers" | "top"

const EVENT_LANES: [string, string][] = [["cooldown", "Healing cooldowns"], ["personal", "Personal"], ["trinket", "Your trinkets"],
                                         ["hit", "Big group hits"], ["death", "Deaths"], ["finding", "Findings"]]
const GAP = 4             // seconds of empty space drawn between pulls (the real gaps are squeezed out)
const LANE_H = 24, HP_H = 170, MANA_H = 48, LEFT = 118, RIGHT = 14
const iconUrl = (icon: string) => `https://wow.zamimg.com/images/wow/icons/medium/${encodeURIComponent(icon)}.jpg`
const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim()
const pct = (v?: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`)
const curveOf = (q: TPull, base: Base) => (base === "top" ? q.study_curve : q.top_curve)

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
  // Which pull a chart position falls in (gaps count to the nearer pull).
  const pullAtC = (x: number) => pulls.findIndex((p, i) => x >= p.start_s + offs[i] - GAP / 2 && x <= p.end_s + offs[i] + GAP / 2)
  return { toC, fromC, pullAtC, total: Math.max(1, c - GAP), pullAt: (s: number) => pulls.findIndex((p) => s >= p.start_s && s <= p.end_s) }
}
type Scale = ReturnType<typeof squeeze>

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

const esc = (s: string) => s.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]!)
const tipHtml = (lines: string[]) => `<b>${esc(lines[0] || "")}</b>${lines.slice(1).map((l) => `<br>${esc(l)}`).join("")}`

export type TimelineHandle = { focus: (s: number, e?: number | null) => void }

// The run at a glance (overview strip: click a pull, drag a range), then the chart of what's open: HP, your events,
// each player's defensives. Drag across the chart to select a range: it zooms there and summarizes it.
export const Timeline = forwardRef<TimelineHandle, { res: Analysis; onPull?: (p: TPull | null) => void }>(function Timeline({ res, onPull }, ref) {
  const t = res.timeline as TimelineData
  const box = useRef<HTMLDivElement>(null), section = useRef<HTMLElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  const sc = useMemo(() => squeeze(t.pulls), [t])
  const [pull, setPull] = useState(() => { const v = +(lsGet(`pull:${res.run.dungeon}:${t.pulls[0]?.start_s}`) ?? -1); return v < t.pulls.length ? v : -1 })
  const [zoom, setZoom] = useState<[number, number] | null>(null)  // run seconds
  const [mark, setMark] = useState<[number, number] | null>(null)
  const [sel, setSel] = useState<[number, number] | null>(null)
  const [party, setParty] = useState(lsGet("tl2:party") !== "0")
  const [minor, setMinor] = useState(lsGet("tl2:minor") === "1")
  const [mana, setMana] = useState(lsGet("tl2:mana") === "1")
  const hasTop = !!res.preview || t.pulls.some((q) => q.study_curve)
  const [base, setBase] = useState<Base>(lsGet("baseline") === "top" && hasTop ? "top" : "peers")
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"))
  const [readout, setReadout] = useState("")

  useEffect(() => {  // re-colour the chart when the theme changes
    const o = new MutationObserver(() => setDark(document.documentElement.classList.contains("dark")))
    o.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] })
    return () => o.disconnect()
  }, [])

  // What the chart shows: a zoom, else the open pull, else the whole run (chart positions).
  const view: [number, number] = zoom ? [sc.toC(zoom[0]), sc.toC(zoom[1])]
    : pull >= 0 ? [sc.toC(t.pulls[pull].start_s), sc.toC(t.pulls[pull].end_s)] : [0, sc.total]
  useEffect(() => { onPull?.(pull >= 0 ? t.pulls[pull] : null) }, [])  // a remembered pull filters on open, too  // eslint-disable-line react-hooks/exhaustive-deps
  const openPull = (i: number) => {
    setPull(i); setZoom(null); setMark(null); setSel(null)
    onPull?.(i >= 0 ? t.pulls[i] : null)
    lsSet(`pull:${res.run.dungeon}:${t.pulls[0]?.start_s}`, String(i))
  }
  const focus = (s: number, e?: number | null) => {
    const end = e ?? s, pad = e != null ? Math.max(3, (end - s) * 0.15) : 15
    let i = sc.pullAt(s)
    if (i < 0) i = t.pulls.reduce((best, p, k) => (Math.abs(p.start_s - s) < Math.abs(t.pulls[best].start_s - s) ? k : best), 0)
    const p = t.pulls[i]
    setPull(i); setSel(null)
    setZoom([Math.max(p.start_s, s - pad), Math.min(p.end_s, end + pad)])
    setMark([s, end])
    section.current?.scrollIntoView({ behavior: "smooth", block: "start" })
  }
  useImperativeHandle(ref, () => ({ focus }))
  const resetZoom = () => { setZoom(null); setMark(null); setSel(null) }

  const lanes = t.lanes || []
  const groupSpans = useMemo(() => groupDefenses(t.defense || [], lanes), [t])  // eslint-disable-line react-hooks/exhaustive-deps
  const partyRows = party ? [...(groupSpans.length ? [{ id: "group" as const, name: "Group", class: "" }] : []), ...lanes] : []
  const hpTop = 30, eventsTop = hpTop + HP_H + 18, partyTop = eventsTop + EVENT_LANES.length * LANE_H + 14
  const partyBottom = party ? partyTop + partyRows.length * LANE_H : eventsTop + EVENT_LANES.length * LANE_H
  const showMana = mana && !!t.mana?.some((v) => v != null)
  const manaTop = partyBottom + 16
  const height = (showMana ? manaTop + MANA_H : partyBottom) + 30
  const lastGrid = showMana ? 3 : party ? 2 : 1

  // Data that only changes with the run (and the party / baseline toggles).
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
    // The comparison band: p25 as an invisible base, p75-p25 stacked on it and filled; the median dashed.
    const lo: [number, number | null][] = [], range: [number, number | null][] = [], med: [number, number | null][] = []
    for (const q of t.pulls) {
      const cv = curveOf(q, base)
      if (!cv) continue
      for (const [f, a, m, b] of cv) {
        const x = sc.toC(q.start_s + f * (q.end_s - q.start_s))
        lo.push([x, a]); range.push([x, b - a]); med.push([x, m])
      }
      const x = sc.toC(q.end_s) + 0.01
      lo.push([x, null]); range.push([x, null]); med.push([x, null])
    }
    const rowOf = (id: Span["lane"]) => partyRows.findIndex((r) => r.id === id)
    const spans = [...groupSpans, ...(t.defense || []).filter((d) => d.scope !== "group")]
      .filter((d) => (minor || !d.minor) && rowOf(d.lane) >= 0 && sc.pullAt(d.start_s) >= 0)
    return { group: line(t.group), lowest: line(t.lowest), mana: line(t.mana || []), lo, range, med, spans, rowOf }
  }, [t, sc, party, minor, groupSpans, base])  // eslint-disable-line react-hooks/exhaustive-deps

  const option = useMemo(() => {
    void dark
    const C = { text: css("--foreground"), muted: css("--muted-foreground"), grid: css("--border"), card: css("--card"),
                primary: css("--data"), good: css("--good"), warn: css("--warn"), bad: css("--bad"), high: css("--high"), low: css("--low") }
    const span = view[1] - view[0]
    const plotW = Math.max(200, (box.current?.clientWidth || 900) - LEFT - RIGHT)
    const fits = (c0: number, c1: number, text: string) => (Math.min(c1, view[1]) - Math.max(c0, view[0])) / span * plotW > text.length * 6.5 + 8
    const dense = span > 240  // zoomed out: plain marks instead of spell icons, which would pile up
    const visible = (s: number) => sc.pullAt(s) >= 0
    const shade = (a: number, b: number, color: string, name = "", bold = false) => [
      { xAxis: sc.toC(a), itemStyle: { color }, name, label: { show: !!name && fits(sc.toC(a), sc.toC(b), name),
        color: bold ? C.text : C.muted, fontWeight: bold ? 700 : 400, fontSize: 11, position: [3, -15] as [number, number] } },
      { xAxis: sc.toC(b) }]
    const areas: ReturnType<typeof shade>[] = []
    const whole = pull < 0 && !zoom
    t.pulls.forEach((p) => {
      const name = (p.chain ? "⛓ " : "") + pullShort(p.label).split(" (")[0]
      areas.push(shade(p.start_s, p.end_s, p.boss ? `${C.primary}1a` : `${C.muted}0d`, whole || !p.phases?.length ? name : "", !!p.boss))
      if (!whole) (p.phases || []).forEach((ph, i) => areas.push(shade(ph.start_s, Math.min(ph.end_s, p.end_s), i % 2 ? `${C.primary}14` : "transparent", ph.name)))
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
    const xAxis = [0, 1, 2, 3].map((i) => ({
      type: "value" as const, gridIndex: i, show: i < 3 || showMana, min: 0, max: sc.total, splitLine: { show: false }, axisTick: { show: false },
      axisLine: { show: false }, axisLabel: { show: i === lastGrid, color: C.muted, formatter: (v: number) => fmtClock(sc.fromC(v)), hideOverlap: true },
      axisPointer: { show: true, snap: false, lineStyle: { color: C.muted, opacity: 0.5 }, label: { show: false } },
    }))
    const laneAxis = (gridIndex: number, rows: string[], color?: (i: number) => string) => ({
      type: "category" as const, gridIndex, data: rows, inverse: true, axisTick: { show: false }, axisLine: { show: false },
      splitLine: { show: true, lineStyle: { color: C.grid, opacity: 0.6 } }, boundaryGap: true,
      axisLabel: { color: color ? (_: string, i: number) => color(i) : C.muted, fontSize: 12, fontWeight: 500, width: LEFT - 16, overflow: "truncate" as const },
    })
    const chains = (t.markers || []).filter((m) => m.kind === "chain" && visible(m.t_s))
    return {
      animation: false,
      textStyle: { fontFamily: "inherit" },
      grid: [
        { left: LEFT, right: RIGHT, top: hpTop, height: HP_H },
        { left: LEFT, right: RIGHT, top: eventsTop, height: EVENT_LANES.length * LANE_H },
        { left: LEFT, right: RIGHT, top: partyTop, height: Math.max(1, partyRows.length) * LANE_H },
        { left: LEFT, right: RIGHT, top: manaTop, height: MANA_H },
      ],
      xAxis,
      yAxis: [
        { type: "value" as const, gridIndex: 0, min: 0, max: 1, interval: 0.5, axisLabel: { color: C.muted, formatter: (v: number) => `${v * 100}%` },
          splitLine: { lineStyle: { color: C.grid } }, name: "HP", nameLocation: "middle" as const, nameGap: 60, nameTextStyle: { color: C.muted, fontWeight: 600 } },
        laneAxis(1, EVENT_LANES.map(([, l]) => l)),
        laneAxis(2, partyRows.map((r) => r.name), (i) => (partyRows[i]?.id === "group" ? C.text : CLASS_COLORS[partyRows[i]?.class] || C.muted)),
        { type: "value" as const, gridIndex: 3, show: showMana, min: 0, max: 1, interval: 1, axisLabel: { color: C.muted, formatter: (v: number) => `${v * 100}%` },
          splitLine: { lineStyle: { color: C.grid } }, name: "Your mana", nameLocation: "middle" as const, nameGap: 60, nameTextStyle: { color: C.muted, fontWeight: 600 } },
      ],
      axisPointer: { link: [{ xAxisIndex: "all" }], triggerTooltip: false },
      tooltip: { trigger: "item" as const, confine: true, backgroundColor: C.card, borderColor: C.grid, textStyle: { color: C.text, fontSize: 12 },
                 formatter: (p: { data: { tip?: string[] } }) => (p.data?.tip ? tipHtml(p.data.tip) : "") },
      // Zoom is driven by the pull picker, the overview and selections only - no wheel or drag zooming.
      dataZoom: [{ type: "inside" as const, xAxisIndex: [0, 1, 2, 3], zoomOnMouseWheel: false, moveOnMouseWheel: false, moveOnMouseMove: false,
                   startValue: view[0], endValue: view[1] }],
      brush: { xAxisIndex: [0, 1, 2, 3], brushType: "lineX" as const, brushMode: "single" as const, transformable: false,
               brushStyle: { color: `${C.primary}22`, borderColor: C.primary, borderWidth: 1 }, outOfBrush: { colorAlpha: 1 } },
      series: [
        { name: "Band base", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.lo, stack: "band", showSymbol: false, silent: true,
          lineStyle: { opacity: 0 }, z: 1 },
        { name: "Band", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.range, stack: "band", showSymbol: false, silent: true,
          lineStyle: { opacity: 0 }, areaStyle: { color: C.muted, opacity: 0.16 }, z: 1 },
        { name: "Band median", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.med, showSymbol: false, silent: true,
          lineStyle: { color: C.muted, type: "dashed" as const, width: 1.5 }, z: 1 },
        { name: "Group HP", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.group, showSymbol: false, sampling: "lttb" as const,
          lineStyle: { width: 2, color: C.primary }, itemStyle: { color: C.primary }, silent: true, z: 3,
          markArea: { silent: true, data: areas },
          markLine: { silent: false, symbol: "none",
                      data: [
                        ...chains.map((m) => ({ xAxis: sc.toC(m.t_s), tip: m.tip, lineStyle: { color: m.risky ? C.warn : C.muted, type: "dashed" as const, width: 2 },
                                                label: { show: !whole, formatter: "⛓ chained in", position: "insideEndTop" as const, color: m.risky ? C.warn : C.muted, fontWeight: 700 } })),
                        ...(t.lust || []).filter((l) => visible(l.t_s)).map((l) => ({ xAxis: sc.toC(l.t_s), tip: [`${l.label} at ${fmtClock(l.t_s)}`],
                          lineStyle: { color: C.high, width: 1.5, opacity: 0.8 }, label: { show: true, formatter: "⚡", position: "end" as const, color: C.high } })),
                      ] } },
        { name: "Lowest player", type: "line" as const, xAxisIndex: 0, yAxisIndex: 0, data: data.lowest, showSymbol: false, sampling: "lttb" as const,
          lineStyle: { width: 1.5, color: C.high }, itemStyle: { color: C.high }, silent: true, z: 2 },
        { name: "Events", type: "scatter" as const, xAxisIndex: 1, yAxisIndex: 1, data: ev.map(evItem), z: 4, emphasis: { scale: 1.3 } },
        ...(party ? [
          { name: "Defensives", type: "custom" as const, xAxisIndex: 2, yAxisIndex: 2, z: 3,
            data: data.spans.map((d) => ({ value: [sc.toC(d.start_s), sc.toC(d.end_s), data.rowOf(d.lane)], tip: d.tip })),
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
        ...(showMana ? [{ name: "Mana", type: "line" as const, xAxisIndex: 3, yAxisIndex: 3, data: data.mana, showSymbol: false, silent: true,
                          lineStyle: { width: 2, color: C.primary }, areaStyle: { color: C.primary, opacity: 0.1 } }] : []),
      ],
    }
  }, [t, sc, data, view[0], view[1], mark, party, showMana, dark, pull, zoom, lastGrid, eventsTop, partyTop, manaTop, partyRows])  // eslint-disable-line react-hooks/exhaustive-deps

  const viewRef = useRef({ pull, zoom }); viewRef.current = { pull, zoom }
  useEffect(() => {
    const c = echarts.init(box.current!)
    chart.current = c
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(box.current!)
    c.on("updateAxisPointer", (e: unknown) => {
      const info = (e as { axesInfo?: { axisDim: string; value: number }[] }).axesInfo?.find((a) => a.axisDim === "x")
      if (!info) return
      const s = sc.fromC(info.value), b = Math.round(s / t.bin_s), p = t.pulls[sc.pullAt(s)]
      const ph = p?.phases?.find((x) => s >= x.start_s && s < x.end_s)
      const cv = p && curveOf(p, baseRef.current), top = cv?.[Math.max(0, Math.min(cv.length - 1, Math.round((s - p.start_s) / (p.end_s - p.start_s) * (cv.length - 1))))]
      setReadout(p ? `${fmtClock(s)} · ${pullShort(p.label).split(" (")[0]}${ph && ph.name !== "Start" ? ` · ${ph.name}` : ""} · Group ${pct(t.group[b])} · Lowest ${pct(t.lowest[b])}`
                     + (top ? ` · ${baseRef.current === "top" ? "Top players" : "Peers"} ${pct(top[2])}` : "") : "")
    })
    c.on("brushEnd", (e: unknown) => {
      const r = (e as { areas: { coordRange: number[] }[] }).areas[0]?.coordRange
      c.dispatchAction({ type: "brush", areas: [] })
      if (!r || Math.abs(r[1] - r[0]) < 1) return
      const a = sc.fromC(Math.min(r[0], r[1])), b = sc.fromC(Math.max(r[0], r[1]))
      if (b - a < 1) return
      setSel([a, b]); setZoom([a, b]); setMark(null)  // zoom to exactly the selection
    })
    c.on("click", (p: unknown) => {
      const m = (p as { data?: { m?: Marker } }).data?.m
      if (m?.kind === "finding" && m.index != null) reveal(`finding-${m.index}`)
      if (m?.kind === "death") reveal(deathId(fmtClock(Math.floor(m.t_s)), m.label.replace(/ died$/, "")))
    })
    // Whole run: a click on a pull opens it. Double-click steps back out (zoom -> pull -> whole run).
    c.getZr().on("click", (e: { offsetX: number; offsetY: number; target?: unknown }) => {
      const { pull, zoom } = viewRef.current
      if (e.target || pull >= 0 || zoom) return
      const [x] = c.convertFromPixel({ gridIndex: 0 }, [e.offsetX, e.offsetY]) as number[]
      const i = sc.pullAtC(x)
      if (i >= 0) openPull(i)
    })
    c.getZr().on("dblclick", () => {
      const { pull, zoom } = viewRef.current
      if (zoom) resetZoom(); else if (pull >= 0) openPull(-1)
    })
    return () => { ro.disconnect(); c.dispose(); chart.current = null }
  }, [sc, t])  // eslint-disable-line react-hooks/exhaustive-deps
  const baseRef = useRef(base); baseRef.current = base

  useEffect(() => {
    const c = chart.current
    if (!c) return
    c.setOption(option, { replaceMerge: ["series"] })
    c.resize()
    // Dragging across the chart selects a range.
    c.dispatchAction({ type: "takeGlobalCursor", key: "brush", brushOption: { brushType: "lineX", brushMode: "single" } })
  }, [option])

  const toggle = (k: string, v: boolean, set: (v: boolean) => void) => { lsSet(k, v ? "1" : "0"); set(v) }
  const levels = (res.compared?.levels || []).map((l) => `+${l}`).join("/")
  const study = ((res.compared?.model || "").match(/\d+/g) || []).map(Number)
  const studyLabel = study.length ? `+${Math.min(...study)}${Math.max(...study) > Math.min(...study) ? `-${Math.max(...study)}` : ""}` : ""
  const open = pull >= 0 ? t.pulls[pull] : null
  const bandNote = res.preview ? "Peer and top-player bands appear when the comparison is done"
    : open && !curveOf(open, base) ? (base === "top" ? "No top-player data for this pull" : "No peer data for this pull")
    : open && base === "top" && open.study_approx ? "Top-player band from the most similar pack (approximate)" : ""

  return (
    <section ref={section} className="flex scroll-mt-18 flex-col gap-4 border-t pt-7">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold tracking-tight">Timeline</h2>
        <select className="h-8 max-w-72 rounded-md border bg-background px-2 text-sm" aria-label="Pull" value={pull} onChange={(e) => openPull(+e.target.value)}>
          <option value={-1}>Whole run</option>
          {t.pulls.map((p, i) => <option key={i} value={i}>{p.label} ({fmtClock(p.start_s)})</option>)}
        </select>
        {zoom ? <button className="text-sm text-primary hover:underline" onClick={resetZoom}>{pull >= 0 ? "Show whole pull" : "Show whole run"}</button>
          : pull >= 0 && <button className="text-sm text-primary hover:underline" onClick={() => openPull(-1)}>Show whole run</button>}
        <div className="ml-auto flex flex-wrap items-center gap-3 text-sm">
          <div className="flex rounded-lg border p-0.5 text-xs" role="group" aria-label="Compare with">
            {([["peers", `Peers${levels ? ` (${levels})` : ""}`], ["top", `Top players${studyLabel ? ` (${studyLabel})` : ""}`]] as const).map(([k, label]) => (
              <button key={k} disabled={k === "top" && !hasTop} title={k === "top" && !hasTop ? "Not available yet for this dungeon" : undefined}
                      onClick={() => { lsSet("baseline", k); setBase(k) }}
                      className={cn("rounded-md px-2.5 py-1 font-medium disabled:opacity-40", base === k ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground")}>
                {label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={party} onChange={(e) => toggle("tl2:party", e.target.checked, setParty)} />Party lanes</label>
          {party && <label className="flex items-center gap-1.5"><input type="checkbox" checked={minor} onChange={(e) => toggle("tl2:minor", e.target.checked, setMinor)} />Minor defensives</label>}
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={mana} onChange={(e) => toggle("tl2:mana", e.target.checked, setMana)} />Mana</label>
        </div>
      </div>
      <Overview t={t} sc={sc} pull={pull} view={view} zoomed={!!zoom} onPull={openPull} onRange={(a, b) => focus(a, b)} />
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><i className="h-0.5 w-4 bg-data" />Group HP</span>
        <span className="flex items-center gap-1.5"><i className="h-0.5 w-4 bg-high" />Lowest player</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-4 rounded-sm bg-muted-foreground/30" />{base === "top" ? "Top players" : "Peers"} (middle half, dashed = median)</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-1 rounded-sm bg-bad" />Big hit, unanswered</span>
        <span>⛓ chain pull · <span className="text-high">⚡</span> Bloodlust</span>
        {party && <>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-data/60" />Personal</span>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-high/60" />External</span>
          <span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-good/60" />Group</span>
        </>}
        {bandNote && <span className="italic">{bandNote}</span>}
        <span className="ml-auto tabular-nums text-foreground">{readout || "Drag across the chart to select a range · double-click to step back out"}</span>
      </div>
      <div ref={box} style={{ height }} className="w-full" />
      {sel && <Selection t={t} sel={sel} base={base} onClose={resetZoom} />}
    </section>
  )
})

// The whole run at a glance, always: pulls side by side with their HP, deaths, chains, lust and the stretches your
// big cooldowns sat ready. Click a pull to open it, drag to zoom into a range, double-click for the whole run.
function Overview({ t, sc, pull, view, zoomed, onPull, onRange }: {
  t: TimelineData; sc: Scale; pull: number; view: [number, number]; zoomed: boolean
  onPull: (i: number) => void; onRange: (a: number, b: number) => void
}) {
  const wrap = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(900)
  const [tip, setTip] = useState<{ x: number; i: number } | null>(null)
  const [drag, setDrag] = useState<[number, number] | null>(null)
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width))
    ro.observe(wrap.current!)
    return () => ro.disconnect()
  }, [])
  const plotW = Math.max(100, W - LEFT - RIGHT), x = (c: number) => LEFT + c / sc.total * plotW
  const xs = (s: number) => x(sc.toC(s)), toC = (px: number) => (px - LEFT) / plotW * sc.total
  const top = 16, hpH = 36, laneH = 13, ready = t.ready_lanes || []
  const readyTop = top + hpH + 6, H = readyTop + ready.length * laneH + 2
  const y = (v: number) => top + (1 - v) * hpH
  const chained = (p: TPull) => (t.markers || []).find((m) => m.kind === "chain" && Math.abs(m.t_s - p.start_s) < 2)
  const px = (e: React.MouseEvent) => e.clientX - wrap.current!.getBoundingClientRect().left
  const up = (e: React.MouseEvent) => {
    if (!drag) return
    const a = Math.min(drag[0], px(e)), b = Math.max(drag[0], px(e))
    setDrag(null)
    if (b - a > 4) onRange(sc.fromC(toC(a)), sc.fromC(toC(b)))
    else { const i = sc.pullAtC(toC(px(e))); if (i >= 0) onPull(i) }
  }
  const tp = tip && t.pulls[tip.i]
  const readyIn = (p: TPull) => ready.map((l) => {
    const inside = l.ready.reduce((s, [a, b]) => s + Math.max(0, Math.min(b, p.end_s) - Math.max(a, p.start_s)), 0)
    return inside >= 30 ? `${l.name} sat ready ${Math.round(inside)}s` : ""
  }).filter(Boolean)
  return (
    <div ref={wrap} className="relative select-none" onMouseLeave={() => { setTip(null); setDrag(null) }}>
      <svg width={W} height={H} className="block cursor-pointer" onDoubleClick={() => onPull(-1)}
           onMouseDown={(e) => { e.preventDefault(); setDrag([px(e), px(e)]) }} onMouseUp={up}
           onMouseMove={(e) => {
             const p = px(e)
             if (drag) { setDrag([drag[0], p]); setTip(null); return }
             const i = sc.pullAtC(toC(p)); setTip(i >= 0 ? { x: p, i } : null)
           }}>
        <text x={8} y={top + 14} className="fill-muted-foreground text-xs font-semibold">Whole run</text>
        {t.pulls.map((p, i) => {
          const x0 = xs(p.start_s), x1 = xs(p.end_s), ch = chained(p), label = pullShort(p.label).split(" (")[0]
          const pts: string[] = []
          for (let b = Math.floor(p.start_s / t.bin_s); b <= Math.min(t.group.length - 1, Math.ceil(p.end_s / t.bin_s)); b += 3)
            if (t.group[b] != null) pts.push(`${xs(b * t.bin_s).toFixed(1)},${y(t.group[b]!).toFixed(1)}`)
          const lx = x0 + (ch ? 14 : 3)
          return (
            <g key={i}>
              <rect x={x0} y={top} width={Math.max(1, x1 - x0)} height={hpH} rx={3}
                    className={cn(p.boss ? "fill-data/15" : "fill-muted-foreground/10", tip?.i === i && "fill-data/25")} />
              {i === pull && !zoomed && <rect x={x0} y={top - 1} width={x1 - x0} height={hpH + 2} rx={3} fill="none" className="stroke-data" strokeWidth={1.5} />}
              <polyline points={pts.join(" ")} fill="none" className="stroke-data" strokeWidth={1.2} />
              {ch && x1 - x0 > 14 && <text x={x0 + 2} y={top - 4} className={cn("text-[11px]", ch.risky ? "fill-warn" : "fill-muted-foreground")}>⛓</text>}
              {x1 - lx > label.length * 6.2 && <text x={lx} y={top - 4} className={cn("fill-muted-foreground text-[11px]", p.boss && "fill-foreground font-bold")}>{label}</text>}
              {!!p.deaths && <text x={x1 - 3} y={top + hpH - 4} textAnchor="end" className="fill-bad text-[11px] font-bold">✕{p.deaths}</text>}
            </g>
          )
        })}
        {zoomed && <rect x={x(view[0])} y={top} width={Math.max(2, x(view[1]) - x(view[0]))} height={hpH} className="fill-data/25" pointerEvents="none" />}
        {(t.markers || []).filter((m) => m.kind === "death").map((m, i) =>
          <line key={`d${i}`} x1={xs(m.t_s)} x2={xs(m.t_s)} y1={top} y2={top + hpH} className="stroke-bad" strokeWidth={1.5} pointerEvents="none" />)}
        {(t.markers || []).filter((m) => m.kind === "cooldown").map((m, i) =>
          <rect key={`c${i}`} x={xs(m.t_s) - 2} y={top + hpH - 7} width={4} height={7} className="fill-foreground" pointerEvents="none" />)}
        {(t.lust || []).map((l, i) => <text key={`l${i}`} x={xs(l.t_s)} y={top + 12} textAnchor="middle" className="fill-high text-xs" pointerEvents="none">⚡</text>)}
        {ready.map((ln, i) => (
          <g key={`r${i}`} pointerEvents="none">
            <text x={8} y={readyTop + i * laneH + 9} className="fill-muted-foreground text-[10px]">{ln.name} ready</text>
            <rect x={LEFT} y={readyTop + i * laneH} width={plotW} height={laneH - 4} className="fill-muted/60" />
            {ln.ready.filter(([a]) => sc.pullAt(a) >= 0 || sc.pullAt(a + 1) >= 0).map(([a, b], k) =>
              <rect key={k} x={xs(a)} y={readyTop + i * laneH} width={Math.max(1, xs(b) - xs(a))} height={laneH - 4} className="fill-good/70" />)}
          </g>
        ))}
        {drag && <rect x={Math.min(...drag)} y={0} width={Math.abs(drag[1] - drag[0])} height={H} className="fill-data/15" pointerEvents="none" />}
      </svg>
      {tp && (
        <div className="pointer-events-none absolute z-10 max-w-sm rounded-md border bg-popover px-3 py-2 text-xs shadow-md"
             style={{ left: Math.min(tip!.x + 12, W - 300), top: top + hpH + 4 }}>
          <b>{tp.label}</b>
          <div className="text-muted-foreground">{fmtClock(tp.start_s)}-{fmtClock(tp.end_s)} ({Math.round(tp.end_s - tp.start_s)}s) · {tp.deaths || 0} death{tp.deaths === 1 ? "" : "s"}</div>
          {(tp.death_lines || []).map((d, i) => <div key={i} className="text-bad">✕ {d}</div>)}
          {tp.verdict && <div>{tp.verdict}</div>}
          {tp.avoid && <div className="text-warn">⚠ {tp.avoid}</div>}
          {tp.chain && <div>⛓ {tp.chain}</div>}
          {readyIn(tp).length > 0 && <div className="text-muted-foreground">{readyIn(tp).join(" · ")}</div>}
          <div className="mt-1 text-muted-foreground">Click to open · drag to zoom into a range</div>
        </div>
      )}
    </div>
  )
}

// What happened in a dragged range: headline numbers as tiles, then what was pressed, used and went wrong in it.
function Selection({ t, sel: [a, b], base, onClose }: { t: TimelineData; sel: [number, number]; base: Base; onClose: () => void }) {
  const b0 = Math.max(0, Math.floor(a / t.bin_s)), b1 = Math.min(t.group.length - 1, Math.ceil(b / t.bin_s))
  const inPull = (s: number) => t.pulls.some((q) => s >= q.start_s && s <= q.end_s)
  const bins: number[] = []
  for (let k = b0; k <= b1; k++) if (inPull(k * t.bin_s)) bins.push(k)
  const pullsIn = t.pulls.filter((q) => q.end_s >= a && q.start_s <= b)
  const fight = pullsIn.reduce((s, q) => s + Math.max(0, Math.min(b, q.end_s) - Math.max(a, q.start_s)), 0)
  const secs = Math.max(1, fight)
  const mean = (xs: number[]) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null)
  const avg = mean(bins.map((k) => t.group[k]).filter((x): x is number => x != null))
  const lowest = Math.min(...bins.map((k) => t.lowest[k] ?? 1))
  // The comparison groups at the same points of each overlapping pull (their median), averaged.
  const topVals: number[] = []
  for (const q of t.pulls) {
    const cv = curveOf(q, base)
    if (!cv || q.end_s < a || q.start_s > b) continue
    for (let s = Math.max(a, q.start_s); s <= Math.min(b, q.end_s); s += t.bin_s)
      topVals.push(cv[Math.max(0, Math.min(cv.length - 1, Math.round((s - q.start_s) / (q.end_s - q.start_s) * (cv.length - 1))))][2])
  }
  const top = mean(topVals)
  const intake = bins.reduce((s, k) => s + (t.intake?.[k] || 0), 0)
  const inFrac = bins.reduce((s, k) => s + (t.intake_frac?.[k] || 0), 0) / secs
  const avoidBy: Record<string, number> = {}
  for (const [k, n, amt] of t.avoid_hits || []) if (k >= b0 && k <= b1) avoidBy[n] = (avoidBy[n] || 0) + amt
  const avoid = Object.values(avoidBy).reduce((s, x) => s + x, 0)
  const avoidNames = Object.entries(avoidBy).sort((x, y) => y[1] - x[1]).slice(0, 3).map(([n]) => n)
  const casts = bins.reduce((s, k) => s + (t.casts?.[k] || 0), 0)
  const inside = (s: number) => s >= a && s <= b
  const marks = (kinds: string[]) => (t.markers || []).filter((m) => kinds.includes(m.kind) && inside(m.t_s))
  const defs: Record<string, number> = {}
  for (const d of (t.defense || []).filter((d) => inside(d.start_s))) defs[d.name] = (defs[d.name] || 0) + 1
  const groupDefs = groupDefenses((t.defense || []).filter((d) => inside(d.start_s)), t.lanes || []).length
  const name = (id: number) => t.lanes?.find((l) => l.id === id)?.name || "?"
  const deaths = marks(["death"]), findings = marks(["finding"])
  const label = base === "top" ? "top players" : "peers"
  const worse = top != null && avg != null && avg < top - 0.03

  const Tile = ({ k, v, sub, tone }: { k: string; v: string; sub?: React.ReactNode; tone?: string }) => (
    <div className="rounded-lg border bg-card px-3 py-2">
      <div className="text-xs text-muted-foreground">{k}</div>
      <div className={cn("text-xl font-semibold tabular-nums", tone)}>{v}</div>
      {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
    </div>
  )
  const rows: [string, React.ReactNode[]][] = [
    ["Your cooldowns", marks(["cooldown", "personal", "trinket"]).map((m) => <Chip key={`${m.label}${m.t_s}`}>{m.label} <Tx>{fmtClock(m.t_s)}</Tx></Chip>)],
    ["Defensives pressed", Object.entries(defs).map(([n, c]) => <Chip key={n}>{n}{c > 1 && <span className="text-muted-foreground"> ×{c}</span>}</Chip>)],
    ["Potions, healthstones & trinkets", (t.uses || []).filter((u) => inside(u.t_s)).map((u, i) =>
      <Chip key={i}>{u.name} <span className="text-muted-foreground">({name(u.lane)})</span> <Tx>{fmtClock(u.t_s)}</Tx></Chip>)],
    ["Deaths", deaths.map((m, i) => <Chip key={i} tone="text-bad">{m.label} <Tx>{fmtClock(m.t_s)}</Tx></Chip>)],
    ["Findings", findings.map((m, i) => <Chip key={i} onClick={() => reveal(`finding-${m.index}`)}>{m.label}</Chip>)],
  ]
  return (
    <div className="flex flex-col gap-3 rounded-lg border bg-secondary p-4 text-sm">
      <div className="flex items-center gap-3">
        <b>Selection <Tx>{`${fmtClock(a)}-${fmtClock(b)}`}</Tx></b>
        <span className="text-muted-foreground">{pullsIn.length > 1 ? `${pullsIn.length} pulls, ${Math.round(fight)}s in combat (gaps left out)` : `${Math.round(secs)}s`}</span>
        <button className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground" onClick={onClose}>
          <X className="size-4" />Clear
        </button>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Tile k="Group HP (average)" v={pct(avg)} tone={worse ? "text-bad" : undefined}
              sub={<>lowest player {pct(lowest)}{top != null && <> · {label} {pct(top)}</>}</>} />
        <Tile k="Damage taken" v={`${(inFrac * 100).toFixed(1)}%/s`} sub="of group HP per second" />
        <Tile k="Avoidable" v={intake ? pct(avoid / intake) : "-"} tone={intake && avoid / intake >= 0.15 ? "text-warn" : undefined}
              sub={avoidNames.length ? avoidNames.join(", ") : "nothing tagged avoidable"} />
        <Tile k="Your casts" v={`${Math.round(casts / secs * 60)}/min`} sub={`${casts} casts${groupDefs ? ` · ${groupDefs} group defensive${groupDefs > 1 ? "s" : ""}` : ""}`} />
      </div>
      <dl className="grid gap-x-4 gap-y-2 md:grid-cols-[max-content_1fr]">
        {rows.filter(([, xs]) => xs.length).map(([k, xs]) => (
          <div key={k} className="contents">
            <dt className="pt-0.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">{k}</dt>
            <dd className="flex flex-wrap gap-1.5">{xs}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function Chip({ children, tone, onClick }: { children: React.ReactNode; tone?: string; onClick?: () => void }) {
  const cls = cn("rounded-md border bg-card px-2 py-0.5 text-xs", tone, onClick && "text-primary hover:bg-accent")
  return onClick ? <button className={cls} onClick={onClick}>{children}</button> : <span className={cls}>{children}</span>
}
