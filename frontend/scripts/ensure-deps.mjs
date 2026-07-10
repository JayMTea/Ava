// Refuse to build with a stale toolchain. After a dependency bump merges,
// a node_modules installed from the OLD lockfile still typechecks and builds —
// but produces a different dist than CI's `npm ci` rebuild, so every push
// fails frontend-dist-drift (see commit 482daae). npm writes
// node_modules/.package-lock.json on install; deps are in sync when every
// non-optional package in the real lockfile is installed at the same version.
// (Optional platform-specific packages are legitimately absent — don't compare
// the trees wholesale.)
import { readFileSync } from 'node:fs';

let stale = [];
try {
  const lock = JSON.parse(readFileSync('package-lock.json', 'utf8')).packages ?? {};
  const have = JSON.parse(readFileSync('node_modules/.package-lock.json', 'utf8')).packages ?? {};
  stale = Object.entries(lock)
    .filter(([path]) => path)                       // skip the root "" entry
    .filter(([path, meta]) =>
      meta.optional ? (path in have && have[path].version !== meta.version)
                    : (!(path in have) || have[path].version !== meta.version))
    .map(([path]) => path);
} catch {
  stale = ['(no node_modules/.package-lock.json — never installed?)'];
}

if (stale.length) {
  console.error(
    '\n✖ node_modules is out of sync with package-lock.json — run `npm ci` first.\n' +
    "  A stale toolchain builds a dist that CI's frontend-dist-drift check rejects.\n" +
    `  Out of sync: ${stale.slice(0, 5).join(', ')}${stale.length > 5 ? ` … +${stale.length - 5} more` : ''}\n`,
  );
  process.exit(1);
}
