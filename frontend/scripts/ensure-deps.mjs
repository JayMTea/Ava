// Refuse to build with a stale toolchain. After a dependency bump merges,
// a node_modules installed from the OLD lockfile still typechecks and builds —
// but produces a different dist than CI's `npm ci` rebuild, so every push
// fails frontend-dist-drift (see commit 482daae). npm writes
// node_modules/.package-lock.json on install; deps are in sync when every
// non-optional package in the real lockfile is installed at the same version.
// (Optional platform-specific packages are legitimately absent — don't compare
// the trees wholesale.)
import { readFileSync } from 'node:fs';

// Same failure, other half: the lockfile check above cannot see the INTERPRETER.
// `engines` allows `^20.19.0 || >=22.12.0` — three majors — while CI's
// frontend-dist-drift job byte-compares your committed dist against its own
// rebuild. Build on a different major than CI and the diff is not your change,
// it is the toolchain, with no message saying so. .nvmrc is the single pin; both
// CI jobs read it via `node-version-file`.
try {
  const want = readFileSync(new URL('../.nvmrc', import.meta.url), 'utf8').trim();
  const wantMajor = want.replace(/^v/, '').split('.')[0];
  const haveMajor = process.version.replace(/^v/, '').split('.')[0];
  if (wantMajor && haveMajor !== wantMajor) {
    console.error(
      `\n✖ Node ${process.version} but frontend/.nvmrc pins ${want}.\n` +
      "  CI rebuilds dist on the pinned major and byte-compares it against yours,\n" +
      '  so a mismatch shows up as a dist diff you did not make.\n' +
      `  Run \`nvm use\` (or install Node ${want}) and rebuild.\n`,
    );
    process.exit(1);
  }
} catch {
  // No .nvmrc, or an unreadable one: fall through. This guard exists to catch a
  // mismatch, not to make a missing pin fatal.
}

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
