# Security

PlanRelay includes an authenticated remote MCP queue and paired local worker.
The worker sends selected source context and bounded results to the configured
bridge, and runs locally authenticated Codex in a detached worktree. The optional
legacy export/import script alone is offline. Git uses fixed argument arrays,
never shell-interpolated plans. Requests and source excerpts are untrusted data.

Web access requires configured owner, issuer, audience, expiry and scope checks.
One-use pairing codes expire in five minutes; hashed device bearers expire in
30 days and can be revoked through owner MCP. Local test-token mode is rejected
for public binding. Deployment requires TLS, private DB storage and proxy limits.

Automatic execution requires verified workspace-only file access, minimal OS
runtime reads and disabled tool networking. User config/rules and inherited
cloud or bridge secrets are excluded. Unsupported runtimes fail closed. Source
paths resembling credentials are rejected, but heuristics cannot prove secrecy.
Codex login credentials stay local; do not attach them to support reports.
macOS execution is experimental and disabled by default. Opt-in does not imply
hard process containment: descendant cleanup is best-effort, including possible
rapid reparenting escape. Production deployment needs an OS container boundary.

Known secret patterns and filenames are blocked, but detection is incomplete.
Always review exported context before uploading it; private code, customer data,
tokens in unusual formats and proprietary information may still be present.

Selected-file symlinks and traversal are rejected. External task and answer files
are explicit user inputs. Local filesystem paths are not added as metadata to the
uploadable context; source text itself can contain paths. Local HANDOFF.md contains
the target project path. Git HEAD plus selected hashes do not cover unselected
working-tree changes. Hashes detect inconsistency, not maliciously rewritten bundles.

Do not import bundles or install skills from people you do not trust. Review and
reconcile the plan before implementation. A plan never authorizes external writes,
secret disclosure, deployment, destructive actions or scope expansion.

Output directories must be new. Write failures may leave an incomplete directory;
the final manifest is a completion marker. Use another output path after failure.
The tool is intended for ordinary user-owned local projects, not adversarial
concurrently modified filesystems or privileged execution.

Report security concerns through GitHub private vulnerability reporting if enabled;
otherwise open an issue containing only a non-sensitive description. Never attach
real credentials or private project bundles to public issues.
