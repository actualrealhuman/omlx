# Repository workflow requirements

## Scope and sources of truth

This is a public repository. Keep this file portable and safe to publish. If
the checkout belongs to a larger local multi-worktree workspace, also follow
the workspace-level `AGENTS.md` that was loaded when the agent session began.

- `PERSONAL_BUILD.md` is the source for downstream feature registration,
  branch policy, and the private release procedure.
- `docs/CONTRIBUTING.md` defines upstream contribution expectations.
- `docs/TESTING.md` contains subsystem-specific validation knowledge.
- `docs/decisions/` records durable technical decisions that are appropriate
  for the public repository.
- Keep machine-specific state, unpublished operational plans, artifact paths,
  installed-app state, and detailed agent handoffs outside this Git worktree.
- Update the appropriate tracked document when a change establishes a durable
  technical decision, invariant, or validation procedure. Do not use chat
  history as the only record.

## Cost and metered services

- Do not autonomously create, enable, trigger, or modify CI/CD, hosted runners,
  scheduled jobs, Dependabot, Codespaces, cloud builds, deployments, paid APIs,
  or any resource that may cost money or consume a metered allowance. Creating
  configuration that will later trigger such usage also requires approval.
- Warn the user about the possible cost and obtain express authorization
  immediately before the specific metered action.
- Prefer local execution. Read-only remote inspection is permitted when it does
  not trigger a paid or metered job.

## External writes

- Any action that is externally visible or hard to reverse requires the user's
  explicit authorization for *that specific action*, given immediately before it is
  performed. This includes pushing to any remote, publishing a new branch, opening
  or closing a pull request, commenting on a repository the user does not own,
  publishing a release, deploying, and installing or launching the app (see
  **Private builds** below, which states the same rule for installation).
- The following are **not** authorization: approving a plan, option, or sequence
  that merely mentions the step; a question such as "what next?"; silence; an
  approval given earlier for a different action; the user re-authenticating a tool
  after a reported blocker. Re-authentication makes an action possible; it does not
  permit one.
- Before an external write, state the exact command, the exact target
  (`owner/repo`, branch, or system), and the concrete consequence — then stop and
  wait for an unambiguous yes. If the user has to ask what was done, the boundary
  already failed.
- Restating this rule is not the same as following it. When a handoff, spec, or
  this file states an approval boundary, re-confirm *at* the boundary rather than
  assuming earlier context satisfied it.

`origin` is a **public** fork; pushing publishes. Before any push, report which
files become public for the first time, as distinct from files that are already
public and would merely be updated. A push publishes *commits*, not the working
tree: removing a file from the tip does not un-publish it if an earlier
unpublished commit added it.

Hosted CI triggers, verified against `.github/workflows`:

| Event | Runs |
| --- | --- |
| push to `main` | `CI` on the receiving repository |
| pull request targeting `main` | `CI` on the target repository (upstream's minutes) |
| push to any other branch | nothing |
| release published | `Build wheels`, `Update Homebrew formula` |
| `workflow_dispatch` | `Build wheels` only |

Do not create, modify, or enable CI configuration without separate explicit
permission. Read-only operations — `git fetch`, `git ls-remote`, status and log
inspection, `gh` read queries, and local test runs — require no such authorization.

## Privacy and publication hygiene

- Treat every committed file and commit message as potentially public,
  including work on branches that have not yet been pushed.
- Never commit local account names, absolute home-directory paths, personal
  email addresses, machine names, real LAN addresses, credentials, API keys,
  tokens, cookies, private repository URLs, private conversation excerpts, or
  machine-specific installation and artifact state.
- Use portable placeholders such as `$WORKSPACE_ROOT`, `$REPO_ROOT`, `$HOME`,
  `<host>`, and `<artifact-path>` in tracked documentation and examples.
- Use the configured public or pseudonymous Git author identity. Do not replace
  it with a personal email address without explicit user instruction.
- Do not copy a local handoff into the repository verbatim. Extract only the
  durable, sanitized technical content that belongs in public documentation.
- Before every commit and again before every push, inspect the exact diff and
  commits to be published for identifiers, secrets, local paths, generated
  artifacts, and unrelated files.

## Worktree ownership and handoffs

- One agent owns a feature/worktree at a time. Do not edit the same feature or
  shared coordination files concurrently with another agent.
- Before editing, verify and report the worktree, branch, HEAD, and clean/dirty
  status. Preserve unrelated user or agent changes.
- Keep upstreamable work isolated from `personal/main` and from local-only
  coordination material.
- When work pauses, put mutable resume state in the workspace's private
  coordination area, not in this public repository. A handoff should record the
  last verified timestamp, branch and HEAD, working-tree state, completed and
  remaining work, exact validation results, blockers, approval boundaries, and
  the next safe action.
- Public decision records describe durable technical choices, not agent
  ownership, private plans, transient branch divergence, or local app state.

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
