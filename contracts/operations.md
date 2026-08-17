# TAPD Capability P0 Operations

## Platform Neutral

The Tool accepts a caller-provided `TapdTransport`. It does not import an Agent host SDK, read credentials, start an MCP server, or encode a project workspace. Every successful read returns the same envelope: `operation_id`, `status`, `effect`, `data`, `page`, `evidence`, and `error`.

## Shared Authority

The package is owned at `.agents/tools/tapd-capability/`. `skills/tapd/` supplies intent routing and workspace-specific policy; it must call this Tool contract through an adapter rather than reimplement result normalization or effect policy.

## User Context Operations

The MCP adapter exposes `tapd_context_status`, `tapd_context_save`, and `tapd_context_resolve`. They reuse `workspace.list_accessible`; no second credential source or project inventory exists.

- `tapd_context_status()` reads the current credential-scoped profile and validates its default project against the current accessible-project set.
- `tapd_context_save(default_project, tapd_identity, business_role)` validates the project against that set before one package-external local write. A successful result declares `effect=workspace-write` and `tapd_write=false`.
- `tapd_context_resolve(project_hint="", tapd_identity="", business_role="")` deterministically applies explicit hint, saved default, then unique accessible project. Multiple candidates expose business names only. A revoked saved default fails with `SAVED_DEFAULT_OUT_OF_SCOPE`; it never falls back to another project. When both freshly user-confirmed identity fields are supplied, the operation revalidates accessible projects and issues a 15-minute opaque `session_context` bound to that credential and workspace by an integrity signature. Issuance performs no profile write.

Profiles contain only a default project reference/name, TAPD identity, business role, and verification metadata. The identity basis is explicitly `user_confirmed`; it is not represented as an API-verified TAPD identity. Profiles are stored under a credential-fingerprint namespace in both stdio and HTTP modes. Tokens, fingerprints, and storage paths are absent from public results. A different credential cannot enumerate or inherit another profile.

Session contexts contain only the confirmed workspace, identity, role, issued time, and expiry inside a caller-opaque signed value. They are not stored. Tampering, expiry, token rotation, or workspace mismatch is rejected locally before any TAPD read. After local verification, semantic/query operations re-run only `workspace.list_accessible`; revoked access stops with `SESSION_CONTEXT_REVOKED` before workflow or entity reads. `tapd_context_save` remains the only way to persist a default.

Any profile directory/create/write/replace failure is normalized at the public boundary as `PROFILE_WRITE_FAILED` with a stable safe message. Raw operating-system exception text is never returned because it may contain the full data directory, credential fingerprint, or temporary filename.

## Business Semantic Operations

The MCP adapter exposes `tapd_semantic_status`, `tapd_semantic_review`, `tapd_semantic_confirm`, and `tapd_business_query`. The platform-neutral implementation lives in `src/semantics.py` and receives the existing injected read capability.

- Every semantic operation requires an existing credential-matched field baseline. A v1.1 baseline remains readable, but business questions return `SEMANTIC_UNCONFIRMED` until a semantic is confirmed and the context is upgraded to v1.2.
- `tapd_semantic_review` never guesses a tenant status. With no explicit values it returns exact business labels from `workflow.get`; with values it requires exact membership and shows selected and non-selected real work items. The candidate stays in credential/workspace/predicate-scoped process memory.
- `tapd_semantic_confirm` refuses without that review, then performs one package-external local write. The stored record carries a parseable confirmation time, the reviewed live-item basis, and a credential-bound integrity proof. It reports `effect=workspace-write` and `tapd_write=false`; no TAPD mutation operation exists on this path.
- `tapd_semantic_status(workspace_id, session_context="")` and `tapd_business_query(..., session_context="")` prefer a valid session context and fall back to the credential-scoped persistent profile only when no claim is supplied. An invalid supplied claim never falls back. `tapd_business_query` accepts no naked identity or role input.
- `tapd_business_query` only supports the fixed initial question “最近分配给我且未开始测试的需求”. “Me” is the identity from the validated session context or `user_confirmed` profile, never an identity inferred from a token. “Recently” means story `modified desc`, and that definition is echoed in the answer.
- Every query verifies the credential-bound confirmation proof, then re-reads the story workflow and compares a canonical content hash plus the exact raw/display mapping with the confirmed snapshot. Missing/foreign baselines, forged or unconfirmed semantics, missing/mismatched profiles, workflow drift, unknown response shapes, transport failures, and incomplete pagination return an empty `条目` with `needs_review` or `blocked`.
- The query accepts only explicit coherent pagination metadata, enumerates every visible page, and then locally applies exact owner/status predicates. Matches are stably sorted by a parseable `modified` timestamp; an unverifiable time blocks the answer. It reports both the true visible match count and returned count. Raw status codes and internal field identifiers are never emitted.

`tapd_list` remains an internal Python diagnostic function and is not registered as a public MCP tool. Raw results from it, `tapd_workspace`, or `tapd_field_config` must never be forwarded as a user answer or used as the fallback for a failed business query.

## P0 Read Operations

P0 permits `workspace.get`, `schema.get`, `workflow.get`, `entity.get`, `entity.list`, `attachment.list`, and `relation.list`. Every request requires a non-empty `workspace_id`. The transport receives only the typed operation and a copied payload.

## Write Boundary

All `write.*` operations and known TAPD mutation names fail with `WRITE_NOT_IMPLEMENTED` before the transport is called. P0 does not create, update, transition, upload, or delete any TAPD entity. The local `tapd_context_save` and `tapd_semantic_confirm` effects are not TAPD writes and never invoke a TAPD mutation operation.

## HTTPS Staging Deployment Contract

The reviewable staging shape terminates TLS at a dedicated reverse proxy and
publishes the application container only on host loopback. The container listens
on `0.0.0.0:3796` internally because its network boundary is the loopback-only
Compose publication plus the proxy; ports 8080 and 8081 and existing business
domains are outside this package.

Every HTTP request whose exact path is `/mcp` is rejected with 401 unless it
carries a non-empty `Authorization: Bearer ...` header. This edge check happens
before MCP protocol handling. The request-scoped transport then resolves the same
header again for the tool call and has no environment fallback. The service does
not cache, persist, or log the value. DeepTutor remains responsible for injecting
the complete Authorization value from its existing per-user secret store; this
capability adds no credential UI or server-side token configuration.

`GET /healthz` is public and constant. It performs no credential resolution,
configuration read, storage read, or TAPD operation, and emits no runtime detail.
All other health assertions (TAPD reachability, project access, tenant data) stay
behind authenticated MCP tools and must not be folded into liveness.

Runtime state lives only under `TAPD_CAPABILITY_HOME=/data`. The image runs as
numeric non-root UID/GID 10001, supports a read-only root filesystem and tmpfs
`/tmp`, and contains only runtime source plus the pinned MCP dependency. The
complete pre-deployment, log, backup, UAT, and rollback gates are normative in
`deploy/README.md`; the files are a local candidate and do not establish a live
Resource, deployment, UAT, merge, or release.

The image source revision has no default. A release builder must inject the
final, already-created 40-character commit SHA through `SOURCE_REVISION`; the
build fails when it is absent or malformed. The exact value is projected into
both the OCI `org.opencontainers.image.revision` label and
`TAPD_CAPABILITY_SOURCE_REVISION` runtime metadata. Compose likewise requires an
explicit `TAPD_CAPABILITY_SOURCE_REVISION` and must never carry an older source
commit as a fallback.

`deploy/deploy_local.py` is the reviewable deployment state machine. Before any
candidate start it persists the previous immutable image digest, previous source
revision, candidate identity, and a fixed-schema non-sensitive configuration
snapshot. The snapshot records only the canonical Compose identity and reviewed
hash, service, loopback endpoint, persistent-volume name/target, read-only-rootfs
flag, and health URL; it never copies Compose source. Only the exact canonical
`deploy/compose.local.yml` path and reviewed content hash are accepted. Any other
path or content fails before the state directory is created or a runner is called,
so arbitrary environment, env_file, secret, config, label, build-argument, volume,
interpolation, or unknown-field input cannot enter state. It verifies both local
image revision labels, starts the candidate,
and checks constant loopback health. Candidate start/health failure immediately
restores the previous digest and rechecks health; failed restore or failed
post-restore health exits nonzero. A successfully restored previous version also
returns nonzero so recovery is not confused with deployment success. The runner
is injectable for fault tests and has an explicit no-side-effect dry-run mode.
No state can delete, recreate, or roll back `/data`.
