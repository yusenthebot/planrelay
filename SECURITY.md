# Security

PlanRelay reads explicitly selected local text files and writes new local bundles.
It makes no network or model requests. Git is invoked only to read HEAD, using
argument arrays, never a shell. Plans and source excerpts are untrusted data.

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
