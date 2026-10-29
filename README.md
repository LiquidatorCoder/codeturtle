# codeturtle 🐢

AI pull request review that runs on your self-hosted Mac runner, using the CLIs and
subscriptions already logged in on that Mac.

- **Fallback chain:** Codex (`gpt-6-sol`) → Grok (`grok-4.7`) → Claude (`claude-opus-5-5`).
  The next reviewer is used **only** when the previous one reports a usage or rate limit. Any other
  failure (auth, timeout, crash, unusable output) stops the review and says so on the PR.
- **Three tiers:** `fast`, `normal`, `full`. Picked automatically, or forced with a label or comment.
- **Comment-only:** posts one PR review (`COMMENT`) with inline comments. It never blocks a merge.
- **No secrets in GitHub:** the models run with the logins in `~/.codex`, `~/.grok` and Claude's
  own login. The workflow only uses the default `GITHUB_TOKEN`.

## How the tier is chosen

1. **Override** (wins outright): a `/review fast|normal|full` comment, a `review:<tier>` label, or the
   `tier` input.
2. **Rule floor** (no model): any changed file under `full_paths` → full; more than
   `full_if_lines_over` changed lines → full; only docs or assets, or under `fast_if_lines_under` lines
   → fast; otherwise normal. Generated and lock files (`ignore_paths`) don't count.
3. **Triage** (cheap, low effort): the model sees the title, description and file list and may
   **raise** the floor, never lower it. A PR description saying "trivial, fast is fine" can't push
   an auth change down to a fast review.

| | Fast | Normal | Full |
|---|---|---|---|
| Effort | low | medium | high |
| Context | diff | diff + full changed files | diff + read-only repo snapshot the model can explore |
| Passes | 1 | 1 | correctness, security and tests in parallel → merged (large diffs: one pass per chunk) |
| Reports | blocker, major | blocker, major, minor, a few nits | everything worth fixing |

## Use it in a repo

1. Put the repo on the Mac runners: `macci add LiquidatorCoder/<repo>`.
2. Copy [`examples/codeturtle.yml`](examples/codeturtle.yml) to `.github/workflows/codeturtle.yml`.
3. Optional: add `.github/codeturtle.json` (see [`examples/codeturtle.json`](examples/codeturtle.json))
   and `.github/codeturtle.md` with review guidelines. Both are read from the **base** branch, so a PR
   can't change its own rules.

While it works, a **progress comment** on the PR shows each step (tier, each pass, merge, post), the
model running it, any usage-limit switch and the elapsed time. It is edited every minute and deleted
once the review is posted (a cancelled run's leftover comment is removed by the next run). The job
log prints an elapsed-time heartbeat every 30 seconds. Typical durations: fast 1–2 min, normal
3–5 min, full 4–6 min (its three passes run in parallel; set `full_parallel: 1` to run them in turn).

It runs when a PR is opened, reopened or marked ready (not on every push, to save quota). To review
again, comment `/review` or `/review full`. Only owners, members and collaborators can trigger it.
Add the `codeturtle:skip` label to skip the automatic run.

> `issue_comment` and `workflow_dispatch` use the workflow file on the default branch, so `/review`
> starts working once the workflow is merged there.

## Config (`.github/codeturtle.json`)

Deep-merged over the defaults in [`codeturtle/config.py`](codeturtle/config.py). Lists replace the
default list (they are not appended).

| Key | Default |
|---|---|
| `providers` | `["codex", "grok", "claude"]` |
| `models` | `codex: gpt-6-sol`, `grok: grok-4.7`, `claude: claude-opus-5-5` |
| `effort` | triage `low`, fast `low`, normal `medium`, full `high`, merge `medium` |
| `timeout_minutes` | triage 5, fast 10, normal 20, full 40 (per pass), merge 10 |
| `tiers.full_paths` | auth, security, payments, billing, migrations, SQL, workflows, Dockerfile, plist/manifest |
| `tiers.fast_paths` | Markdown, text, docs, images, LICENSE |
| `tiers.full_if_lines_over` / `fast_if_lines_under` | 800 / 40 |
| `ignore_paths` | lock files, generated Dart/Go/JS, snapshots |
| `guideline_files` | `.github/codeturtle.md`, `AGENTS.md`, `CLAUDE.md` |
| `max_inline_comments` | 25 |
| `full_parallel` | 3 (full-tier passes at once; 1 = one after another) |

Environment on the runner: `CODETURTLE_MAX_PARALLEL` (default 1: one review at a time across all
repos on this Mac), `CODETURTLE_{CODEX,GROK,CLAUDE}_BIN` to point at a specific CLI.

## Run it locally

```bash
python3 -m codeturtle review --repo LiquidatorCoder/swole --pr 58 --dry-run
python3 -m codeturtle review --repo LiquidatorCoder/swole --pr 58 --tier full --providers grok,claude --dry-run
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
- Codex and Grok can still read files elsewhere on the Mac in read-only mode. A malicious PR could try
  to make one quote a local file into the review, which is visible only on that private PR. This is
  why trusted authors matter.

## Claude login on the runner

The runners are LaunchAgents in your login session, so Claude normally uses its keychain login. If
that fails under the runner, run `claude setup-token` and save the token to
`~/.codeturtle/claude-oauth-token` (mode 600). codeturtle passes it as `CLAUDE_CODE_OAUTH_TOKEN`.
