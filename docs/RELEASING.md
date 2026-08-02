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

## Cutting a release

1. **Finalize the changelog.** In `CHANGELOG.md`, rename the `## [Unreleased]`
   heading to `## [X.Y.Z] - YYYY-MM-DD`, and start a fresh empty `[Unreleased]`
   above it. (The release notes are extracted from the matching section by
   `deploy/scripts/changelog_extract.py`; if you forget to rename, it falls back
   to `[Unreleased]`.)
2. **Bump `VERSION`** to `X.Y.Z`. One file, three consumers: the fallback the app
   reads when `AVA_VERSION` isn't injected (`ava_bridge/version.py`), the package
   version (`pyproject.toml` reads it via `dynamic = ["version"]`), and the
   `ava version` output. Optionally sync `frontend/package.json` with
   `npm version X.Y.Z --no-git-tag-version` (cosmetic only - the version never
   reaches `frontend/dist`, so CI's dist-drift job cannot catch an unsynced
   `package.json`, and the bump alone needs no rebuild).
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
v0.0.1-rc1`. It exercises the full build/sign/SBOM/release path - with two
caveats: `release.yml` applies `latest` unconditionally, so the run re-points
both `ghcr.io/jaymtea/ava-bridge:latest` and
`ghcr.io/jaymtea/ava-agent-runtime:latest` at the test build, and the GitHub
Release is created **published, not draft**. Afterwards: delete the tag, delete
the release, delete the rc images from GHCR, and re-point `latest` at the last
real release digest.
