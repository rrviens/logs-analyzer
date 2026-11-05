import { Component, type ReactNode } from "react"

// A rendering bug in one review shows a message there instead of blanking the whole page.
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="rounded-xl border bg-card p-6">
        <h2 className="mb-1 text-lg font-semibold">Couldn't show this run</h2>
        <p className="text-sm text-muted-foreground">Something broke while drawing the review: {this.state.error.message}</p>
      </div>
    )
  }
}
