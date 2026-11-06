# codeturtle 🐢

AI pull request review that runs on your self-hosted Mac runner, using the CLIs and
subscriptions already logged in on that Mac.

- **Fallback chain:** Codex (`gpt-6-sol`) → Grok (`grok-4.7`) → Claude (`claude-opus-5-5`).
  The next reviewer takes over when one hits a usage or rate limit, isn't installed or logged in on
  the runner, or keeps failing with a temporary error (model at capacity, server error, network
  drop) after two retries. Each switch is shown on the PR. Any other failure (timeout, crash,
  unusable output) stops the review and says so on the PR.
- **Health-checked:** every review first checks that each CLI is installed and logged in, and names
  a broken backup on the PR before it is needed (see [Provider health](#provider-health-12)).
- **Three tiers:** `fast`, `normal`, `full`. Picked automatically, or forced with a label or comment.
- **Verified before posting:** every blocker and major is re-checked by a different provider, which
  drops or downgrades what it can't confirm (see [Verification and follow-ups](#verification-and-follow-ups-14)).
- **Follow-ups, not repeats:** a second review of a PR looks only at the new commits, says what was
  fixed, still open and new, and resolves the threads of fixed findings.
- **Security on every review:** a dedicated security pass and an independent verifier, using rules
  adapted from Cloudflare's [security-audit skill](https://github.com/cloudflare/security-audit-skill)
  (see [Security review](#security-review-13)).
- **Comment-only:** posts one PR review (`COMMENT`) with inline comments. It never blocks a merge.
- **No secrets in GitHub:** the models run with the logins in `~/.codex`, `~/.grok` and Claude's
  own login. The workflow only uses the default `GITHUB_TOKEN`.

## How the tier is chosen

1. **Override** (wins outright): a `/review fast|normal|full` comment, a `review:<tier>` label, or the
   `tier` input.
2. **Rule floor** (no model): any changed file under `full_paths` → full; more than
   `full_if_lines_over` changed production lines → full; only tests, docs or assets, or under
   `fast_if_lines_under` production lines → fast; otherwise normal. Production lines leave out tests
   and fixtures (`test_paths`), docs and assets (`fast_paths`) and generated or lock files
   (`ignore_paths`).
3. **Triage** (cheap, low effort): the model sees the title, description and file list and may
   **raise** the floor, never lower it. A PR description saying "trivial, fast is fine" can't push
   an auth change down to a fast review.

| | Fast | Normal | Full |
|---|---|---|---|
| Effort | low | medium | high |
| Context | diff | diff + full changed files | diff + read-only repo snapshot the model can explore |
| Passes | 1 | 1 | correctness, security and tests in parallel → merged (large diffs: one pass per chunk) |
| Security | dedicated pass on the diff | dedicated pass that reads the repo | dedicated pass that reads the repo |
| Reports | blocker, major | blocker, major, minor, a few nits | everything worth fixing |

Every tier also sends its security findings to an independent verifier before posting them.

## Use it in a repo

1. Put the repo on the Mac runners: `macci add LiquidatorCoder/<repo>`.
2. Copy [`examples/codeturtle.yml`](examples/codeturtle.yml) to `.github/workflows/codeturtle.yml`.
   From then on it is kept current for you (see [Keeping every repo current](#keeping-every-repo-current)).
3. Optional: add `.github/codeturtle.json` (see [`examples/codeturtle.json`](examples/codeturtle.json)),
   `.github/codeturtle.md` with review guidelines and `.github/codeturtle-security.md` with the repo's
   threat model (see [Security review](#security-review-13)). All are read from the **base** branch,
   so a PR can't change its own rules.

While it works, a **progress comment** on the PR shows each step (tier, each pass, merge, post), the
model running it, any usage-limit switch and the elapsed time. It is edited every minute and deleted
once the review is posted (a cancelled run's leftover comment is removed by the next run). The job
log prints an elapsed-time heartbeat every 30 seconds. Typical durations: fast 1–2 min, normal
3–5 min, full 4–6 min (its three passes run in parallel; set `full_parallel: 1` to run them in turn).

It runs when a PR is opened, reopened or marked ready (not on every push, to save quota). To review
again, comment `/review` or `/review full`: that reviews only the commits since the last review (a
follow-up). `/review fresh` (or `/review fresh full`) reviews the whole PR again. Only owners, members and collaborators can trigger it.
Add the `codeturtle:skip` label to skip the automatic run.

> `issue_comment` and `workflow_dispatch` use the workflow file on the default branch, so `/review`
> (and dispatched reviews) start working once the workflow is merged there.

## Keeping every repo current

Nothing here is done by hand: releasing is merging a version bump, and every repository follows.

- **Code: the `v1` tag.** Repositories use `LiquidatorCoder/codeturtle@v1`.
  [`release.yml`](.github/workflows/release.yml) runs on every push to main: when `__version__` in
  [`codeturtle/__init__.py`](codeturtle/__init__.py) has no tag yet, it runs the tests, tags
  `v<version>` (it must be newer than every release), points `v1` at it and publishes the GitHub
  release. Every repository runs the new version on its next review. A push without a bump does
  nothing; a `v1` left behind an existing release is moved on the next run. A new major (2.0.0) gets
  `v2` and leaves `v1` alone. Releases used to be tagged by hand, which is how 1.2 and 1.3 never
  reached the repositories.
- **Workflow file: the fleet sync.** Each repository's `.github/workflows/codeturtle.yml` is a copy of
  [`examples/codeturtle.yml`](examples/codeturtle.yml) and goes stale when the template changes (1.4
  added the `fresh` input). [`fleet.yml`](.github/workflows/fleet.yml) runs daily and after every
  release. It finds every repository you can see that has the file (no list to keep: a new repo is
  picked up once the file is on its default branch) and compares it with the latest release's template:

  | Workflow | What happens |
  |---|---|
  | in sync | nothing |
  | outdated: an unmodified older template | a PR (`codeturtle/workflow-sync`, labelled `codeturtle:skip`) updates it and is merged at once. Its commits carry `[skip ci]`, so neither the PR nor the merge starts the repository's GitHub Actions (CI, build or deploy workflows) or a review. A host that deploys through its own GitHub app, such as Vercel, ignores `[skip ci]` and redeploys the unchanged app |
  | pinned: an older template on another ref (`@v1.1.0`, a SHA) | reported, never changed |
  | customized: anything else | reported, never changed |
  | opted out: the file has a `# codeturtle-sync: off` line | reported, never changed |

  The job summary lists every repository with the version it runs, and the run fails when one needs
  you (pinned, customized, public, no write access, an update or merge GitHub refused) or when `v1`
  is behind the latest release (then nothing is written), so GitHub emails you. A template that fails
  the protocol check (`# codeturtle-protocol: 2`, one `@v1` action, the harness's dispatch inputs) is
  never rolled out.
- **Its token.** The sync writes to other repositories, which a workflow's own `GITHUB_TOKEN` can't.
  It uses the `CODETURTLE_FLEET_TOKEN` secret when set (a classic token with `repo` and `workflow`, or
  a fine-grained one with Contents, Pull requests and Workflows read/write), else the runner's own
  `gh` login. Both workflows run only main's code (no `pull_request` trigger).
- **Mode.** `sync` (default) merges, `pr` leaves the PRs for you, `check` only reports: pick one when
  running it by hand (Actions → fleet → Run workflow), or set the repository variable
  `CODETURTLE_FLEET_MODE`.

```bash
python3 -m codeturtle fleet --mode check                 # what every repository runs, from a terminal
python3 -m codeturtle fleet --mode pr --repo LiquidatorCoder/edie
python3 -m codeturtle release --dry-run                  # what the next push to main would tag
```

## Verification and follow-ups (1.4)

- **Blockers and majors are verified before posting.** In the harness every blocker or major is
  fixed automatically and nothing reviews the fix, so a wrong finding becomes an unreviewed change.
  One extra call, on a provider other than the one that wrote the findings when one is healthy,
  re-reads the code for each blocker and major (`verify.severities`) and tries to refute it.
  - **Confirmed** findings are posted, never above the severity it confirmed.
  - **Rejected** findings are dropped.
  - Findings that **hinge on a fact outside the repo** become leads with no severity.
  The same call verifies the security candidates. Minors and nits are posted as written. The review
  shows `Verified by Claude (claude-opus-5-5): 3 blocker and major findings · 2 confirmed · 1 dropped`.
- **Follow-up reviews.** Each review hides its state in its body: the reviewed commit and its findings
  with ids. Each inline comment carries its finding's id. When the PR is reviewed again and its new
  head descends from that commit, codeturtle:
  - reviews only the PR files changed since;
  - re-checks the earlier findings in those files, at the same time;
  - reports `Since abc1234: ✅ 3 fixed · 🔁 1 still open · 🆕 2 new`;
  - lists still-open findings instead of re-posting them;
  - replies to and resolves the threads of fixed ones.
  Ids are carried forward, so they survive line moves, and `result.json` `findings` is the current
  state (open plus new). A rewritten history (force push), `/review fresh`, the `fresh` input or
  `"incremental": false` reviews the whole PR. Only the workflow bot's own reviews count as state.
- **Less quota per review.**
  - Only production lines count toward the size rules.
  - When the rules choose `full` only because some files are sensitive (`tiers.split_full`), those
    files get the full passes and the rest one normal-depth pass. The security pass still sees
    everything.
  - The merge step runs at low effort.
- **How often findings get fixed:**

  ```bash
  python3 -m codeturtle stats --repo LiquidatorCoder/swole --repo LiquidatorCoder/edie --prs 50
  ```

  It reports the share of inline findings that were addressed (the line changed in a later commit,
  or the thread was resolved) by severity, category, tier, codeturtle version and repository. Use it
  to measure prompt, tier or model changes.

## Security review (1.3)

The security rules are adapted for PR review from Cloudflare's MIT-licensed
[security-audit skill](https://github.com/cloudflare/security-audit-skill) (notice in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)). The skill audits a whole repository in six phases
(map, hunt, challenge, record, verify, report). A review has one diff and a few model calls, so
[`codeturtle/security.py`](codeturtle/security.py) keeps what carries over:

- **The evidence bar.** A security finding names the lower-trust actor and what they control, the
  control that fails, the boundary crossed and the concrete impact. Missing best practices,
  defense-in-depth gaps behind a working control, self-impact and guessed deployment behaviour are
  not findings. Every review prompt carries this bar.
- **A dedicated security pass on every review** (except docs- or assets-only changes), in parallel
  with the main pass. It maps the entry points and trust boundaries the change touches, hunts with the
  skill's attack classes (injection, access control, files and SSRF, secrets and crypto, business
  logic, feature abuse, obvious exposures), and challenges each candidate before reporting it. Fast
  reviews give it the diff; normal and full reviews let it read the repository snapshot.
- **Domain checks picked from the changed files:** AI and agents, HTTP and identity, browser code,
  supply chain and CI, cloud and deployment, RPC and webhooks, resource exhaustion and spend, data
  isolation, mobile and local IPC, memory safety (at most `max_companions`, default 4).
- **An independent verifier.** Each security candidate, including security findings from the other
  passes, goes to a fresh call that reads the snapshot and tries to refute it. Its verdicts:
  - `confirmed`: posted as a finding. Severity comes from the demonstrated impact (critical or high →
    blocker, medium → major, low → minor).
  - `needs_validation`: the path is real but one fact outside the repository decides it, such as a
    proxy, identity provider or cloud setting. It is listed under "Security leads that need
    validation" with that fact, has no severity, and is not in `findings`, so nothing auto-fixes it.
  - `rejected`: dropped, and counted in the review's security line.
  If the verifier can't run, the candidates are posted unverified and the review says so. If the
  security pass fails, the review is still posted, with a note.
- **The repo's threat model.** `.github/codeturtle-security.md` (from the base branch) tells the
  security pass and the verifier what the trust boundaries are, for example "tenants never share
  rows" or "only the billing service may call /internal".

Each review body has a line such as
`Security: dedicated pass (HTTP, sessions and identity) · 1 confirmed · 1 need validation · 2 rejected by an independent check`,
and `result.json` has a `security` object with the same counts and the leads.

**Cost:** fast and normal reviews make one extra call (the security pass), plus one verifier call
when there are security candidates. Full reviews already had a security pass; they add the verifier
call. A fast review with security candidates also downloads the snapshot for the verifier.

## Provider health (1.2)

- **Checked before every review.** Each CLI's own status command (`codex login status`,
  `claude auth status`, `grok models`: no model call, no quota) runs in the reviews' environment.
  A provider that isn't installed or logged in is tried last, and the progress comment, the review
  and `result.json` name it with the fix. A broken backup shows up on the next PR, not on the day
  the main reviewer runs out.
- **Usage limits are remembered.** The reset time in a limit message ("try again at 7:39 PM",
  "try again in 20 seconds") is saved in `~/.codeturtle/limits.json`, and until then later reviews
  try that provider last instead of hitting the limit again (30 minutes when no time is given,
  never more than 12 hours). The review footer shows `usage limit: Codex (until 7:39 PM IST)`.
- **Short limits are waited out.** When every provider is out and one resets within
  `limit_wait_minutes` (default 30), the review waits for it instead of failing. Otherwise the
  failure comment and `result.json` (`retry_at`) say when to try again.
- **GitHub blips are retried** (after 5 s, 15 s and 45 s): reads and idempotent writes on network
  or 5xx errors, other writes only when the request never left the Mac.
- `python3 -m codeturtle doctor` runs the same check from a terminal and exits 1 when a provider
  can't review. It sees your terminal's login session; the check inside each review shows what the
  runner sees.

## Machine-readable result (1.1)

Every run uploads one artifact, `codeturtle-result-attempt-<run_attempt>`, holding `result.json`
(`schema: "codeturtle.result"`, `schema_version: 1`, see [`codeturtle/result.py`](codeturtle/result.py)):
repository, PR number, base/head refs and the **reviewed head SHA**, the run id/attempt/event, the
caller's `request_id`, `status` (`completed`, `failed`, `usage_limited`, `skipped`), the tier and why,
which providers ran (and how many model calls), the summary, every finding with a stable id
(`ct-` + 12 hex of path, line, severity, title) and where it was posted, `findings_total` and
per-severity `counts`, and the posted review's id/URL. Since 1.2 it also has `retry_at` (for
`usage_limited`: the earliest reset a provider reported) and, under `reviewers`, `limits`,
`unavailable` and the health check's result per provider. Since 1.4, `verification` says what
verification did to the blockers and majors, `followup` lists the fixed, still-open and obsolete ids
of a follow-up, and a still-open earlier finding has `placement: "earlier"`. Since 1.3, `security` holds the security
check's counts, whether it was verified, and its `needs_validation` leads (never in `findings`).
These are additive, so `schema_version` stays 1. It is redacted like the PR review and never holds a token, an environment variable, a
prompt or model reasoning. The PR review and inline
comments are unchanged; the artifact is for tools.

A tool that consumes it must first check through the GitHub API that the artifact belongs to a
**successful** run of `.github/workflows/codeturtle.yml` whose workflow file is identical to the
default branch's (a `pull_request` run uses the PR's copy of the workflow, and a `workflow_dispatch`
can name any ref), and that its repository, PR, head SHA and run match what it asked for. The Claude
Code harness does exactly this before it signs a review.

## Callers: minimum tier and request ids (1.1)

- `workflow_dispatch` inputs `min_tier` and `request_id`, or the action inputs `min-tier` /
  `request-id`. `min_tier` **raises** the rule floor (a triage model may raise it further); it never
  lowers it, and a `/review fast` override is lifted to it too. `request_id` (1-128 of
  `A-Za-z0-9._:-`) is echoed in the result and in the dispatched run's title
  (`codeturtle PR <n> <request_id>`), so a caller can find the run it started.
- A PR description containing `<!-- codeturtle:min-tier=full -->` raises the automatic PR-open review
  the same way (it can only make the review deeper).
- The workflow template declares `# codeturtle-protocol: 2`; tools check it before dispatching.

## Config (`.github/codeturtle.json`)

Deep-merged over the defaults in [`codeturtle/config.py`](codeturtle/config.py). Lists replace the
default list (they are not appended).

| Key | Default |
|---|---|
| `providers` | `["codex", "grok", "claude"]` |
| `models` | `codex: gpt-6-sol`, `grok: grok-4.7`, `claude: claude-opus-5-5` |
| `effort` | triage `low`, fast `low`, normal `medium`, full `high`, merge `low`, verify `medium` |
| `verify.severities` | `["blocker", "major"]` (re-checked before posting; `[]` posts everything as written) |
| `verify.different_provider` | `true` (verify on a provider other than the one that wrote the findings) |
| `incremental` | `true` (a repeat review follows up on the last one; `/review fresh` overrides) |
| `timeout_minutes` | triage 5, fast 10, normal 20, full 40 (per pass), merge 10, verify 15 |
| `security.pass` | `auto` (dedicated pass unless only docs or assets changed), `always` or `off` |
| `security.verify` | `true` (an independent call tries to refute each security finding before it is posted) |
| `security.rules_files` | `[".github/codeturtle-security.md"]` (the repo's threat model, from the base branch) |
| `security.max_companions` | 4 (domain checks picked from the changed files) |
| `limit_wait_minutes` | 30 (when every reviewer is out, wait this long at most for a usage limit to reset; 0 fails at once) |
| `tiers.full_paths` | auth, security, payments, billing, migrations, SQL, workflows, Dockerfile, plist/manifest |
| `tiers.fast_paths` | Markdown, text, docs, images, LICENSE |
| `tiers.full_if_lines_over` / `fast_if_lines_under` | 800 / 40 changed production lines |
| `tiers.test_paths` | test dirs and `*_test.*`, `*.test.*`, `*.spec.*`, `test_*.py`, `*Tests.swift`, … (don't count toward size) |
| `tiers.split_full` | `true` (full passes only for the sensitive files when they alone made it full) |
| `ignore_paths` | lock files, generated Dart/Go/JS, snapshots |
| `guideline_files` | `.github/codeturtle.md`, `AGENTS.md`, `CLAUDE.md` |
| `max_inline_comments` | 25 |
| `full_parallel` | 3 (full-tier passes at once; 1 = one after another) |

Environment on the runner: `CODETURTLE_MAX_PARALLEL` (default 1: one review at a time across all
repos on this Mac), `CODETURTLE_{CODEX,GROK,CLAUDE}_BIN` to point at a specific CLI,
`CODETURTLE_HOME` (default `~/.codeturtle`: review slots, `limits.json`, the Claude token file).

## Run it locally

```bash
python3 -m codeturtle review --repo LiquidatorCoder/swole --pr 58 --dry-run
python3 -m codeturtle review --repo LiquidatorCoder/swole --pr 58 --tier full --providers grok,claude --dry-run
python3 -m codeturtle doctor
python3 -m codeturtle stats --repo LiquidatorCoder/swole --prs 50
python3 -m codeturtle review --repo LiquidatorCoder/swole --pr 58 --fresh --dry-run
python3 -m codeturtle fleet --mode check
python3 -m unittest discover -s tests -t .
```

`--dry-run` writes the review payload to `out/` instead of posting it.

## Security model

A self-hosted runner executes on your Mac with your AI logins, so:

- **Private repos with trusted authors only.** Fork PRs are refused by the job's `if` and again by
  codeturtle. Never enable this on a public repo, and keep "Run workflows from fork pull requests" off
  (the private-repo default): a fork PR may edit the workflow itself, so only that setting stops it.
- `/review` comments from users without write access never start a job, so they can't cancel a
  running review through `concurrency`.
- **The PR is untrusted input.** Prompts mark PR text and code as data. Models run read-only:
  Codex `--sandbox read-only` (no network), Claude `--restricted` with only `Read,Grep,Glob` (full tier)
  or no tools, Grok `--permission-mode plan` with web search off.
- **The PR can't configure the reviewer.** The full-tier snapshot is a tarball of the PR head with
  agent config removed (`.claude/`, `.codex/`, `.grok/`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`, …).
  Guidelines come from the base branch.
- **No GitHub token reaches a model.** The CLIs run with a minimal allow-listed environment; only the
  posting step uses `GITHUB_TOKEN`. Anything that looks like a key or token is redacted before posting.
- `workflow_dispatch` and `issue_comment` always run the **default branch's** workflow; a
  `pull_request` run uses the PR's copy, and anyone with write access can dispatch from another ref.
  A consumer of the result artifact must therefore verify the run's workflow file (see above).
- Other workflows on the same self-hosted runners (your CI) execute PR code with your login session:
  the trust boundary for every runner on this Mac is "people with write access to the repo".
- Codex and Grok can still read files elsewhere on the Mac in read-only mode. A malicious PR could try
  to make one quote a local file into the review, which is visible only on that private PR. This is
  why trusted authors matter.

## Claude login on the runner

The runners are LaunchAgents in your login session, so Claude normally uses its keychain login. If
that fails under the runner, run `claude setup-token` and save the token to
`~/.codeturtle/claude-oauth-token` (mode 600). codeturtle passes it as `CLAUDE_CODE_OAUTH_TOKEN`.
