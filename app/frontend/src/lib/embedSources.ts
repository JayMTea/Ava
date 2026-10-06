/**
 * A frame saying that the view it renders already lists its own sources, so the
 * chart around it should not list them again (docs/CONNECTOR_SDK.md §3).
 *
 * The saved artifact can say the same (`chart.sources_in_view`), but only for
 * charts saved after their connector learned to; the view says it as it renders,
 * so an old chart is covered too. Accepted like every frame message: only from
 * this frame's own window, and only from the app's origin. The worst a lying app
 * can do is hide its own citations, which it could equally leave out.
 */
export function frameShowsSources(event: MessageEvent, frame: Window | null | undefined, origin: string): boolean {
  return !!frame && event.source === frame && event.origin === origin
    && event.data?.type === 'ava:sources-in-view';
}
