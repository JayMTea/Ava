/** The right-click menu's shape, statically â€” the same `react-dom/server`
 *  route Drawer.test.tsx takes, for the same reason: the SPA has no
 *  component-render harness, and the ARIA contract is about the element tree. */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ContextMenuList } from './ContextMenu';

const ACTIONS = [
  { label: 'Open in new tab', icon: 'external', onClick: () => {} },
  { label: 'Open in new window', icon: 'window', onClick: () => {} },
  { label: 'Copy link', icon: 'copy', onClick: () => {} },
];

function render(cursor: number) {
  return renderToStaticMarkup(
    <ContextMenuList
      label="App A â€” open inâ€¦"
      actions={ACTIONS}
      cursor={cursor}
      itemRefs={{ current: [] }}
      menuRef={{ current: null }}
      style={{ left: 10, top: 20 }}
      onPick={() => {}}
      onKeyDown={() => {}}
    />,
  );
}

describe('the right-click menu', () => {
  it('is a named menu of menuitems, positioned where it was asked to be', () => {
    const html = render(0);
    expect(html).toContain('role="menu"');
    expect(html).toContain('aria-label="App A â€” open inâ€¦"');
    expect(html.match(/role="menuitem"/g)).toHaveLength(ACTIONS.length);
    expect(html).toContain('left:10px');
    expect(html).toContain('top:20px');
  });

  it('keeps exactly one item in the tab order, the cursor', () => {
    // Roving tabindex: Tab must land on one item, and arrows do the rest.
    for (const cursor of [0, 1, 2]) {
      const html = render(cursor);
      expect(html.match(/tabindex="0"/g)).toHaveLength(1);
      expect(html.match(/tabindex="-1"/g)).toHaveLength(ACTIONS.length - 1);
    }
  });

  it('wears the flyout chrome, so it reads as the same system as the foot menu', () => {
    expect(render(0)).toContain('class="rail-menu ctx-menu"');
    expect(render(0).match(/class="rail-menu-item"/g)).toHaveLength(ACTIONS.length);
  });
});
