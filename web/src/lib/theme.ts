import { useEffect, useState } from "react"
import { lsGet, lsSet } from "./wow"

// Same setting as the classic page ("theme": system / dark / light); dark when the OS says nothing.
export type ThemePick = "system" | "dark" | "light"
const os = window.matchMedia("(prefers-color-scheme: light)")

function apply(pick: ThemePick) {
  const dark = pick === "dark" || (pick === "system" && !os.matches)
  document.documentElement.classList.toggle("dark", dark)
}

export function useTheme(): [ThemePick, (p: ThemePick) => void] {
  const stored = lsGet("theme")
  const [pick, setPick] = useState<ThemePick>(stored === "dark" || stored === "light" ? stored : "system")
  useEffect(() => {
    apply(pick)
    const follow = () => apply(pick)
    os.addEventListener("change", follow)
    return () => os.removeEventListener("change", follow)
  }, [pick])
  return [pick, (p) => { lsSet("theme", p); setPick(p) }]
}
