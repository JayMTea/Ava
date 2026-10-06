import { Component, Fragment, type ReactNode } from 'react';

// Contains a render crash to the view that threw it. Without this, one bad
// field in an API response (a view reading `undefined.something`) unmounts the
// entire shell — drawer, header, and every kept-alive app iframe included.
// Conditionally-rendered views unmount the boundary on tab switch, so error
// state clears itself; persistent children (kept-alive AppFrames) recover via
// the retry button, which remounts only the crashed child.
export class ViewErrorBoundary extends Component<
  // `hidden` suppresses the error panel while the crashed child belongs to an
  // inactive tab (kept-alive AppFrames stay in the tree across tab switches —
  // their error panel must not paint over whichever view is actually active).
  { label: string; hidden?: boolean; children: ReactNode },
  { error: Error | null; epoch: number }
> {
  state = { error: null as Error | null, epoch: 0 };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`[${this.props.label}] view crashed:`, error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="view-error" style={this.props.hidden ? { display: 'none' } : undefined}>
          <div className="view-error-title">{this.props.label} hit an error</div>
          <div className="view-error-detail">{String(this.state.error)}</div>
          <button type="button"
            className="st-btn"
            onClick={() => this.setState((s) => ({ error: null, epoch: s.epoch + 1 }))}
          >
            Try again
          </button>
        </div>
      );
    }
    // epoch key remounts the child on retry so it refetches from scratch
    // instead of re-rendering the same crashed state. A keyed Fragment adds
    // no DOM node, so absolutely-positioned views (.appframe) keep #viewPort
    // as their containing block.
    return <Fragment key={this.state.epoch}>{this.props.children}</Fragment>;
  }
}
