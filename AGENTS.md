# Repository workflow requirements

## Upstream integration

- `personal/main` is the only canonical source for private release builds.
- Before claiming that the fork is current, fetch `upstream` during the current
  integration task and compare against the freshly updated `upstream/main`.
- Merge upstream into `personal/main` before downstream integration. Preserve
  upstream's `omlx/_version.py` value during conflict resolution.
- Dated integration branches are review workspaces, not release sources.

## Downstream features

- Develop independently upstreamable work on `feature/*` branches based on
  `upstream/main`, then integrate the tested result into `personal/main`.
- Every feature present in the private build must have a stable ID in
  `omlx/_build_manifest.json` and a matching row in `PERSONAL_BUILD.md`.
- Remove the ID and update the documentation when a feature is removed or
  becomes part of upstream.

## Private builds

- Run `apps/omlx-mac/Scripts/build.sh release --preflight-only` before a full
  private release build. Do not bypass a failed preflight for a distributable
  build.
- A canonical release requires a clean `personal/main`, inclusion of the local
  `upstream/main`, and the same package version as that upstream ref.
- Keep release artifacts in the script's revision-specific default directory.
  Do not reuse a known-good artifact's output directory.
- After building, verify the signature and embedded version, build number,
  channel, source revision/branch, and feature IDs before installation.
- Never install, replace, or launch the user's current app unless the user has
  explicitly requested that separate action.
- For an authorized installation, use
  `apps/omlx-mac/Scripts/install_build.py --app /path/to/oMLX.app --yes`; do not
  hand-roll process termination or copy directly over `/Applications/oMLX.app`.
- Run the installer with `--dry-run` first. Preserve its rollback backup and
  report both the installed identity and backup path when the operation ends.

See `PERSONAL_BUILD.md` for the full branch policy and release checklist.
