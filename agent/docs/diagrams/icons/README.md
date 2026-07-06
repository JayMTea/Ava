# Diagram icons

Vendored [Lucide](https://lucide.dev) line icons (ISC License), recolored to
slate `#334155` for a consistent, professional, monochrome look across every
generated diagram. Kept **local** (not CDN) so the diagram pipeline renders
fully offline and reproducibly — remote icon URLs would fail the pre-commit
render.

- Source: `lucide-static` (https://github.com/lucide-icons/lucide), ISC License.
- Referenced by filename stem from `agent/docs/architecture.yaml` (`icon:` fields)
  and resolved by `agent/docs/arch.py` as `icons/<stem>.svg` relative to each
  generated `.d2`.
- To add an icon: drop `<name>.svg` here, recolor `currentColor` → `#334155`,
  then reference `icon: <name>` in the manifest.
