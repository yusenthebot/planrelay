# GitHub private mailbox workflow

No hosted bridge, Feishu account, custom web MCP app or new OAuth server is
required for this mode. Web ChatGPT uses its **actually connected GitHub tools**.
Local Codex uses the installed skill, a project-scoped read-only MCP companion,
and the user's existing `gh` login. Web authorization is separate from local
login; connecting GitHub and selecting the private repo still requires user action.

## One-prompt setup in Codex

For the three copyable entry prompts, see [prompts](prompts.md). The same private
repo remains bound across tasks; each received Issue has its own mailbox branch.

With the plugin installed, say:

> Use GPT Connector to connect this project through my private GitHub mailbox
> OWNER/gpt-connector-inbox. Upload only README.md and docs/spec.md. I authorize
> writing those selected files and task results to that mailbox. Generate the
> web onboarding prompt, and do not enable unattended execution.

Replace the owner and upload list with real values. The skill obtains missing
project/repository/upload authorization before publishing; it does not pick a
business project or upload the whole filesystem. It can create the named private
mailbox only with the user's explicit approval. Configuration stores no token.

Equivalent command:

```sh
uv run gpt-connector github setup --repository OWNER/gpt-connector-inbox \
  --project /absolute/project --project-id my-project \
  --file README.md --file docs/spec.md --allow-write
```

Use a clean primary Git repo with a committed HEAD. Private config defaults to
`~/.config/gpt-connector/github.json`; override using
`GPT_CONNECTOR_GITHUB_CONFIG` or `--config`. Do not commit this local binding.

## Web-first day-to-day flow

Paste the generated `web_prompt` into the web chat. It checks actual GitHub file
read, Issue creation and Issue/comment read capability. If writes are missing,
setup is incomplete: an ordinary prompt cannot install a connector or grant access.
The prompt does not look for `pair_device` or a published GPT Connector web app.

The context is `gpt-connector/context.json` in the mailbox. After planning and
explicit user approval, the web assistant creates an Issue whose title begins
`[GPT Connector] ` and whose body is JSON or a single JSON code block:

```json
{
  "protocol": "gpt-connector/v1",
  "project_id": "my-project",
  "revision": "ACTUAL_64_CHARACTER_REVISION_FROM_CONTEXT",
  "request": "Original user requirement",
  "plan": "Approved implementation and acceptance criteria",
  "approved": true
}
```

Then in Codex say:

> Use GPT Connector to receive mailbox Issue #12, implement the approved scope
> in this bound project, run the relevant tests, and write the result back. Do
> not commit, push, deploy or access unrelated systems.

This single local instruction authorizes development and result publication for
that Issue. Remote `approved: true` alone is not independent execution authority.
The skill runs `github start --issue N --allow-write` to validate the task and
automatically create or reuse `gpt-connector/task-N` in the mailbox repo. It pins
`gpt-connector/context.json` and `gpt-connector/task.json` on that branch; verified
results are archived at `gpt-connector/result.json` and published as Issue comments.
The branch is created on local receipt, not when the web assistant creates an
Issue. Web branch-write tools are not required. Different Issues get different
branches; repeated receipt uses the original branch and rejects changed records.
These are mailbox branches, not automatic business-code pushes.

The skill reads and validates the task, develops using the current Codex task's
normal permissions, verifies, and writes a bounded result comment. **This mode
does not use the older HTTP worker's sandbox and does not auto-start a model.**

Manual equivalents:

```sh
uv run gpt-connector github status
uv run gpt-connector github next --issue 12
uv run gpt-connector github start --issue 12 --allow-write
uv run gpt-connector github result --issue 12 \
  --body-sha256 ACTUAL_BODY_HASH_FROM_NEXT \
  --summary-file /absolute/private/result.md --state succeeded --allow-write
```

`next` only reads data. `start` writes mailbox transport records but never starts
a model or edits local code. Legacy Issues without a task branch remain supported
as comment-only results. `result` only publishes the supplied summary; it does not
certify that tests ran. The skill must describe actual evidence and mark blocked
or failed accurately. Manually refresh shared context after a new clean baseline:

```sh
uv run gpt-connector github sync --allow-write
uv run gpt-connector github prompt
```

## Limits and honest readiness

- Personal private mailbox only; numeric GitHub account/repo IDs are pinned and
  rechecked. Visibility changes, transfers, PRs and other authors are rejected.
- Selected context only: up to 32 files, 128 KiB each, 512 KiB total. Request,
  plan and result each have smaller bounds. Secret heuristics are incomplete:
  inspect upload lists even for private repositories.
- Issue revisions bind tasks to local selected-file hashes and HEAD. Modified,
  closed or stale tasks are rejected. Context writes use Contents API SHA conflict
  checks; task snapshots are pinned on their branches, and result records are
  deduplicated. Never blindly retry uncertain writes. A conflicting archived result
  is not overwritten: use a new task for a changed requirement.
- No background watcher, distributed claims or exactly-once development guarantee.
  Use one active local consumer. Fetching the same Issue does not reserve it.
  No automatic business Git commit/push/deploy. Mailbox writes intentionally create
  API commits on the task branches. Result comments do not close Issues.
- No chat scraping, cookie sharing, credential uploads or GitHub Actions model run.
  Private GitHub is a transport, not a sandbox or a zero-cost model provider.
- Local setup success is **local ready**, not proof the user's web chat connected.
  Full verification requires web read → web Issue creation → local receive →
  local result → web read-back. CLI/mock tests alone do not prove this chain.

Official account/plugin access and actual tools vary; see
[OpenAI's connection guidance](https://developers.openai.com/plugins/deploy/connect-chatgpt).
