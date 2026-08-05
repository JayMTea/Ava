# Releasing Ava

Ava uses **SemVer**, and the **git tag is the source of truth** for the version.
Pushing a `vX.Y.Z` tag triggers `.github/workflows/release.yml`, which builds a
multi-arch image, pushes it to GHCR, signs it keylessly with cosign, attaches an
SBOM + checksums, and creates the GitHub Release. Pre-1.0 tags (`v0.x.y`) signal
"beta - the API may still move."

## One-time setup (maintainers)

- Enable **GitHub Pages** (to publish the docs site) and **Private vulnerability
  reporting** (repo → Settings → Security).
- Ensure your commit/tag signing key is set up (`git config user.signingkey`,
  `commit.gpgsign` or SSH signing). Add the public key to your GitHub account so
  tags show "Verified".
- The release workflow pushes to `ghcr.io/jaymtea/ava-bridge` automatically using
  the built-in `GITHUB_TOKEN` - no secrets to configure. First push may require
  making the package public (repo → Packages → package settings).

## Testing a change before you tag it

Run it in a **slot** - a second Ava beside the running one, with its own database,
port and image tag, so the stable instance keeps serving while you work. Mechanics
and the warnings that go with it are in
[INSTALL_REFERENCE.md §11](INSTALL_REFERENCE.md#11-running-a-second-instance-a-staging-slot).

```bash
git switch -c fix/whatever
# edit, then:
cd deploy && ./slot.sh up -d --build       # open http://127.0.0.1:8097
```

`--build` is not optional - plain `up` reuses the existing `ava/bridge:staging`
tag and you retest yesterday's code. **Setup → System → About** should show the
`+stg.<sha>` stamp `slot.sh` just made; if it ends `.dirty`, the build included
uncommitted changes.

Then: rebuild `frontend/dist` and commit it if you touched `frontend/src` (the
slot cannot catch dist drift - only CI compares the two), `git commit -s` because
DCO is enforced on PRs, push, wait for CI green, merge, and cut the release below.

**What you tested is not byte-for-byte what you ship, and it is worth knowing
where the gap is.** A slot builds your *working tree* with local single-arch
BuildKit against floating base tags (`python:3.12-slim`, `node:22-slim`); the
release is a multi-arch build from the *tagged commit*, signed, with an SBOM. The
working-tree difference is the only part of that worth spending anything on, and
the `+stg.<sha>[.dirty]` stamp addresses it by making the slot confess rather than
by making the local build imitate CI.

## Cutting a release

1. **Finalize the changelog.** In `CHANGELOG.md`, rename the `## [Unreleased]`
   heading to `## [X.Y.Z] - YYYY-MM-DD`, and start a fresh empty `[Unreleased]`
   above it. (The release notes are extracted from the matching section by
   `deploy/scripts/changelog_extract.py`; if you forget to rename, it falls back
   to `[Unreleased]`.)
2. **Bump the version in four files.** `VERSION` is the source of truth, with
   three consumers: the fallback the app reads when `AVA_VERSION` isn't injected
   (`ava_bridge/version.py`), the package version (`pyproject.toml` reads it via
   `dynamic = ["version"]`), and the `ava version` output. Three *other* files
   restate it and are **hard-asserted against it** by
   `tests/test_version_ssot.py` - `CITATION.cff`, `frontend/package.json` and
   `demo/package.json`. Bump all four, then prove it:
   ```bash
   python -m pytest tests/test_version_ssot.py -q
   ```
   No rebuild is needed: the version never reaches `frontend/dist`, which is also
   why CI's dist-drift job cannot catch an unsynced `package.json` - only the test
   above can.
3. **Commit** the changelog + version bump.
4. **Tag, signed, and push:**
   ```bash
   git tag -s vX.Y.Z -m "Ava vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. The release workflow runs. When it finishes, the GitHub Release is created and
   `ghcr.io/jaymtea/ava-bridge:X.Y.Z` (+ `X.Y`, `latest`) is published and
   signed. The image tag drops the `v` - the git tag is `vX.Y.Z`, the image tag
   is `X.Y.Z`.

## Verifying (anyone)

See [SECURITY.md §9](../SECURITY.md#9-verifying-a-release) - `cosign verify`
against the release-workflow OIDC identity, plus the SBOM/provenance attestations.

## Testing the pipeline without a real release

Push a throwaway pre-release tag, e.g. `git tag -s v0.0.1-rc1 && git push origin
v0.0.1-rc1`. It exercises the full build/sign/SBOM/release path without touching
what real users get: a SemVer pre-release is defined by the hyphen, and
`release.yml` keys both its `latest` tag and the GitHub Release's `prerelease:`
flag off exactly that. So `ghcr.io/jaymtea/ava-bridge:latest` and
`ghcr.io/jaymtea/ava-agent-runtime:latest` stay pointed at the last real release,
and the Release is marked as a pre-release rather than announced as the current
one. (Both used to be unconditional, which turned every pipeline test into a
manual cleanup.)

Afterwards, tidy up: delete the tag, delete the release, and delete the rc images
from GHCR. Nothing needs re-pointing.
