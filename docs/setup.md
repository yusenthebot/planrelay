# Service configuration

No hosted service or identity provider is included. Provide an OAuth server
supporting ChatGPT MCP authorization/client onboarding, scope planrelay and
RS256 JWT access tokens. Tokens require iss, sub, iat, exp and aud exactly
matching the public MCP endpoint. Configure your own stable owner subject.
No enterprise-specific login is required. Use any established identity provider
that satisfies this MCP OAuth contract. GitHub may be its upstream login option;
a GitHub OAuth token or local gh credential is not the MCP RS256 access token.
The bridge verifies its own audience/scopes and owner identity, not the local
GitHub username. This repository does not include a hosted GitHub login service.

```sh
export PLANRELAY_PUBLIC_URL=https://relay.example.com/mcp
export PLANRELAY_OAUTH_ISSUER=https://auth.example.com/
export PLANRELAY_JWKS_URL=https://auth.example.com/.well-known/jwks.json
export PLANRELAY_OWNER_SUBJECT=your-stable-subject
export PLANRELAY_DATABASE=/absolute/private/relay.sqlite
export PLANRELAY_HOST=127.0.0.1
export PLANRELAY_PORT=8787
uv run gpt-connector serve
```

Placeholders are not working URLs. Put a TLS reverse proxy before this server.
Keep DB, configs and artifacts private, with backups; configure proxy body,
time and rate limits. The endpoint publishes OAuth protected-resource metadata.

Pairing codes expire in five minutes and are one-use. Device tokens expire in
30 days. Owner MCP revoke_device(worker_id) immediately revokes device requests
and blocks running leases. Workers never receive ChatGPT cookies or login data.
Keep worker config mode0600 in a private directory, outside project/artifacts.

## Local tests only

PLANRELAY_LOCAL_TOKEN may contain a random >=32-character test secret only
with literal loopback HTTP URLs/binding. Public binding is rejected. This is
not ChatGPT web OAuth. Never reuse real credentials for integration fixtures.

## Execution

Automatic model execution is disabled by default. Experimental macOS opt-in is
explicit via pair --experimental-execution (experimental_execution in config).
Process-tree identity tracking is best-effort and cannot prove containment of
rapidly reparented/detached processes. Do not use this as a production hard
deadline/security guarantee. A production runner needs an OS container boundary.
Validated CLI version is pinned; unsupported versions block rather than relax.

After revoking a device, pair a replacement and explicitly call owner MCP
rebind_project(project_id, worker_id), then register a fresh selected snapshot.
This invalidates old context and pending jobs; it never silently reuses them.

Local saved Codex ChatGPT login is used. User configuration and inherited cloud
or bridge secrets must not enter model tools. An enforced profile allows only
workspace file access plus minimal system runtime reads and disables tool
networking. Actual runtime probes must pass before model execution. Prepare
trusted dependencies beforehand; never silently loosen permissions.

Context <=512 KiB, individual source <=128 KiB, request <=32 KiB, plan <=512 KiB.
Runs are sequential with bounded time/output. Dirty baseline, unsafe tracked
paths, drift, cancelled/expired leases and unsafe output block rather than retry.
Credential heuristics are incomplete; inspect the selected source upload list.

Original request is authoritative. Plans are advice, not authorization to push,
deploy, invoke external connectors, read login directories or edit other projects.
