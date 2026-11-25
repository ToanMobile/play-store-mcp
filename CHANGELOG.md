<!-- markdownlint-disable-file MD024 -->
<!-- Keep a Changelog repeats "### Added"/"### Changed"/etc. across versions;
     MD024 (no-duplicate-headings) is disabled for this file by design. -->

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

> **Breaking — code-mode is now enabled by default.** Clients that expect the
> full 117-tool list (e.g. anything enumerating tools by name) will instead
> see three meta-tools (`search`/`get_schema`/`execute`) unless `CODE_MODE=0`
> is set. See Changed below.

### Security (fork)
- HTTP transports (`sse`/`streamable-http`) support bearer-token auth via
  `PLAY_STORE_MCP_AUTH_TOKEN` / `--auth-token` (constant-time check on every
  request except `/health` and `/credentials`). A non-loopback bind without a
  token now refuses to start unless `PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED=1` /
  `--allow-unauthenticated` is set.
- `apk_manager.sh` validates every package name before it reaches `adb shell`
  (XAPK manifest / OBB-derived names could inject device shell commands).
- `/credentials` requires `PLAY_STORE_MCP_ADMIN_TOKEN`, or else
  `PLAY_STORE_MCP_AUTH_TOKEN`, whenever either is set — a loopback peer is no
  longer trusted alone (same-host tunnels such as cloudflared/ngrok/ssh -R arrive
  as 127.0.0.1). The key is now proven live (an access token is minted) before it
  replaces the shared clients; a non-object body is a 400, not a 500.
- Bearer scheme is case-insensitive, tokens are whitespace-stripped (env and
  CLI), `--auth-token ""` is an error, and websocket scopes without a token are
  closed (1008) instead of passed through.
- Credential headers must decode to a non-empty JSON object: a JSON string was
  treated as a server-side key *file path*, and `{}`/`0`/`[]` silently fell back
  to the server's ambient credentials.
- Uploads over an HTTP transport read files only from `PLAY_STORE_MCP_UPLOAD_DIR`
  (a remote caller could otherwise make the server read and send any local file).
- `bigquery_execute_query`'s `max_bytes_billed` is capped by
  `PLAY_STORE_MCP_BIGQUERY_MAX_BYTES_BILLED` (default 10 GB).
- `apk_manager.sh` refuses XAPKs that contain symlinks (`adb push` followed them
  and copied host files to `/sdcard`), and the OBB package fallback works with
  BSD sed. `ADB`/`TARGET_IP`/`TARGET_PORT` can be overridden from the environment.
- Every tool declares MCP `ToolAnnotations`: the 74 write tools
  `destructiveHint`, the rest `readOnlyHint` (tested against the read-only
  inventory).

### Fixed (fork)
- `deploy_app` and `upload_image` use the long upload timeout (was 120 s).
- `rollout_percentage` must be > 0 (0 produced an `inProgress` release with
  `userFraction` 0, which Play rejects).
- A malformed credentials file is a `PlayStoreClientError` (logged at startup)
  instead of an uncaught exception.
- Docs: 8 undocumented tools added to `docs/tools-reference.md`; README env
  table de-duplicated (`CODE_MODE` default is on).

### Changed (fork)
- Synced 42 commits from upstream `lusky3/play-store-mcp` (through #161) on
  2026-09-14. Merge notes: the fork's shared credential loader
  (`play_store_mcp.credentials`, used by all five API clients) is kept and now
  funnels every branch — dict, JSON string, file path — through
  `from_service_account_info` with upstream's `token_uri` SSRF guard (#147),
  so the guard covers Reporting/BigQuery/Analytics/Crashlytics too, not just
  the Publisher client. Upstream's duplicate `_resolve_credentials*` helpers in
  `client.py` were dropped in favour of that loader; the fork's long-timeout
  upload transport and `_service_lock` double-checked init (#150) coexist.

### Fixed
- Artifact uploads no longer hide the server's answer behind a client-side
  timeout. The API transport used googleapiclient's default 60s socket
  timeout, which a large `.aab` upload routinely outruns, so a Play `500
  INTERNAL` surfaced as `Failed to upload bundle: The read operation timed
  out` — a network-shaped message that points diagnosis in the wrong
  direction. Uploads now go over a transport with a much longer timeout
  (`PLAY_STORE_MCP_UPLOAD_TIMEOUT`, default 1200s; other calls use
  `PLAY_STORE_MCP_HTTP_TIMEOUT`, default 120s), a genuine timeout is reported
  as one and says no HTTP status was received, and upload errors now carry the
  status code (`HTTP 500: ...`) rather than the bare reason.

### Added
- `upload_apk` and `upload_bundle` take `commit` (default `true`). With
  `commit=false` the artifact is uploaded and validated by Play, then the edit
  is discarded: no draft on the Console and no version code consumed, so a
  rejected artifact can be told apart from a failing Play backend without
  burning a version code per attempt. The result reports `committed`.
- Added `list_crashlytics_issues` and `get_crashlytics_issue` so a Crashlytics
  `issue_id` can be discovered and verified from the MCP server instead of the
  Firebase console. Crashlytics and Android Vitals identify the same crash
  under different 32-character hex IDs, and the Crashlytics API rejects a
  foreign ID with an opaque `500 INTERNAL`, so IDs from `list_error_issues`
  could not be used with `close_crashlytics_issue`. Listing is backed by the
  v1alpha `topIssues` report (the API has no `issues.list` method) and supports
  title/stack-trace search plus error-type and state filters. Both tools are
  reads and stay available in read-only mode.
- Added `close_crashlytics_issue`, backed by the Firebase Crashlytics v1alpha
  issue `patch` API, to close fatal-crash and Android-ANR issues. It supports
  file, environment, per-request header, and `/credentials` service-account
  credentials and is blocked by read-only mode.

### Changed
- **Breaking:** `CODE_MODE` now defaults to **enabled** (was opt-in/default-off).
  Set `CODE_MODE=0` (or `false`/`no`/`off`, case-insensitive) to opt out and
  get the classic 117-tool list back. The `execute` meta-tool's sandbox
  (Monty) is now a base dependency — not an optional extra — so every
  install path (`pip install play-store-mcp`, `uvx play-store-mcp`, Docker)
  works out of the box with no separate install step. Read-only enforcement
  still applies inside the sandbox.

### Planned
- Consolidate and reduce the MCP tool surface by grouping
  related operations, to lower per-request tool-list overhead — with no planned
  loss of functionality.

### Dependencies
- Bumped `fastmcp` from the `4.0.0b3`/`b5` betas to the stable GA release
  (`>=4.0.0,<5.0`, currently resolving to `4.0.2`), which migrates to MCP
  Python SDK v2 and the new MCP 2026-07-28 spec (stateless protocol core; no
  more mandatory `initialize`/`Mcp-Session-Id`). FastMCP 4 negotiates the
  best mutual protocol era per client by default, so existing clients still
  on the older handshake keep working unchanged — verified locally: full
  unit suite (728 tests), lint, mypy, both transports (`stdio` and
  `streamable-http`) boot and complete a legacy `initialize` →
  `notifications/initialized` → `tools/list` round trip, the live read-only
  integration suite against a real Play Console app, and the Docker build
  all pass unmodified. Now that FastMCP 4 is GA, the `uvx --from git+URL`
  prerelease limitation noted in earlier releases no longer applies —
  regular installs work without `--prerelease=allow`. `stable` will follow
  in its own release once this has soaked.

## [0.5.0] - 2026-08-14

Adds opt-in **code-mode**, migrates the server onto the standalone **`fastmcp`**
package, hardens shared-client concurrency, and removes the non-functional Vitals
tools.

> **Breaking — Vitals tools removed.** `get_vitals_overview` and
> `get_vitals_metrics` no longer exist (see Removed); they returned placeholder
> data and never called an API.

### Added
- **Experimental code-mode (opt-in):** set `CODE_MODE=1` to expose tools through
  FastMCP's code-mode transform (`search`/`get_schema`/`execute` meta-tools with a
  sandboxed executor) instead of the full tool list, reducing per-request tool-list
  token overhead. Off by default; requires the `play-store-mcp[code-mode]` extra
  (Monty sandbox) for the `execute` tool. This is the first step of the tool-surface
  reduction noted under Planned.

### Changed
- Migrated the server framework from the official MCP SDK's `FastMCP`
  (`mcp.server.fastmcp`) to the standalone `fastmcp` package (v3). Behavior is
  unchanged — all 117 tools, the `/health` and `/credentials` routes,
  per-request header credentials, admin-token auth, read-only mode, and
  DNS-rebinding protection (`PLAY_STORE_MCP_DISABLE_DNS_REBINDING`) are
  preserved. This unblocks the upcoming code-mode capability, which lives only
  in `fastmcp`.
- **Breaking:** APK/AAB downloads are now **always confined to a directory** —
  there is no "write anywhere" mode. The base directory is
  `PLAY_STORE_MCP_DOWNLOAD_DIR` when set, otherwise the server's current working
  directory; a `destination_path` that resolves outside it is rejected. On
  network transports (`--transport sse` / `streamable-http`), setting
  `PLAY_STORE_MCP_DOWNLOAD_DIR` is **recommended** but not required — the server
  logs a warning (rather than refusing to start) when it is unset and falls back
  to the working directory. Point it at a writable directory on cloud/hosted
  deployments (e.g. `/tmp/play-store-downloads` on Render), where the working
  directory may be read-only.

### Removed
- **Breaking:** removed the non-functional `get_vitals_overview` and
  `get_vitals_metrics` tools. They never called an API and returned hardcoded
  placeholder data; Android Vitals requires the separate Play Developer
  Reporting API, which is out of scope for this server.

### Fixed
- `get_order` / `batch_get_orders` now read the real v3 `Order` resource:
  product IDs from `lineItems[].productId` (exposed as `product_ids` /
  `line_items`) and status from the `state` string enum. Previously they read
  non-existent top-level `productId` / `purchaseState` fields, so those values
  were always null against the live API and order state was lost.
- `list_in_app_products` now follows `tokenPagination.nextPageToken` instead of
  returning only the first page (apps with many SKUs were silently truncated).
- `get_reviews` and `list_voided_purchases` now paginate to `max_results`
  across pages via `tokenPagination`, rather than returning a single page.
- `delete_subscription_offer` now returns the parent `product_id` instead of
  mislabeling the deleted `offer_id` as `product_id`.
- Media downloads (`download_generated_apk` / `download_system_apk_variant`) now
  acquire the client's transport lock per chunk, closing a gap in the shared-client
  thread-safety fix: a download concurrent with another call on the shared client
  no longer races on the non-thread-safe `httplib2` transport (which could corrupt
  the downloaded file or raise `ResponseNotReady`).
- The shared (env / `/credentials`) client now serializes its HTTP transport with
  a per-client lock, so concurrent tool calls under network transports no longer
  race on the non-thread-safe `httplib2` connection (which could interleave
  requests or deliver a response to the wrong caller). Per-request header-auth
  clients each get their own client and stay fully concurrent.

### Security
- APK/AAB downloads (`download_generated_apk`, `download_system_apk_variant`)
  now write to a temporary file and atomically rename on success, so a failed
  or unauthorized download can no longer truncate an existing file or leave a
  partial one at the destination.
- Download-destination confinement lives in `PlayStoreClient` and applies to both
  the temporary `.part` file and the final file: every destination is canonicalized
  and verified to stay within the (always-present) base directory before anything
  is written, closing the path-traversal / arbitrary-file-overwrite vector
  (SonarCloud `S2083`). Downloads are always confined — `PLAY_STORE_MCP_DOWNLOAD_DIR`
  when set, otherwise the working directory.
- Documented that the server-side credential fallback
  (`GOOGLE_PLAY_STORE_CREDENTIALS` / `/credentials`) is a process-global client
  shared by every request that omits a credential header; multi-tenant
  deployments should leave it unset so a missing header fails closed rather than
  running under a shared identity.
- Recommend pairing code-mode with `--read-only` / `PLAY_STORE_MCP_READ_ONLY=1`
  unless writes are needed: one `execute` can invoke up to 50 tool calls
  (including mutations) behind a single approval. Read-only enforcement still
  applies inside the sandbox.

### Dependencies
- Bumped `pyasn1` 0.6.3 → 0.6.4 (CVE-2026-59885, CVE-2026-59886) and
  `cryptography` 49.0.0 → 50.0.0 (PYSEC-2026-3552) — HIGH-severity advisories in
  transitive dependencies (via `google-auth` / `pyjwt[crypto]`). `pip-audit` clean.

## [0.4.0] - 2026-07-02

Major feature expansion: grows from ~24 to **119 MCP tools**, adding broad
coverage of the Google Play Developer API, plus reliability/security hardening
and a full dependency refresh.

> **Note — write endpoints are beta.** The new write/mutating tools in this
> release are covered by unit tests (mocked), but only read-only paths have been
> exercised against the live Play API. Treat create/update/patch/delete/upload/
> purchase-action/migrate tools as beta and
> [open an issue](https://github.com/lusky3/play-store-mcp/issues) for any
> problems. Run with `--read-only` / `PLAY_STORE_MCP_READ_ONLY=1` to disable all
> write operations.

> **Note — tool count.** 119 tools is a large surface for a single MCP server:
> it increases per-request token usage and some clients cap/truncate large tool
> lists. A follow-up release will reduce this.

### Added
- **Purchases & orders:** in-app product purchases (`get`/`acknowledge`/`consume`);
  purchase management (`refund_order`, `cancel`/`defer`/`revoke_subscription_purchase`,
  `get_product_purchase_v2`); `get_review`, `batch_get_orders`.
- **Monetization catalog:** in-app products, subscriptions, subscription base
  plans (incl. price migration), subscription offers, one-time products, and
  one-time product purchase options & offers.
- **Artifacts & uploads:** edit upload pipeline (APKs, app bundles, deobfuscation
  and expansion files); store-listing images; generated APK list + download;
  system APK variants; internal app sharing uploads.
- **Account & configuration:** external transactions (alternative billing);
  device tier configs; app data safety labels; app recovery actions; Play Console
  account access (users & grants).
- **Read-only mode:** `--read-only` / `PLAY_STORE_MCP_READ_ONLY` disables all write
  operations.

### Changed
- Transient errors (429/500/503) are retried with exponential backoff on real API
  calls, and the retry is idempotency-aware — non-idempotent (POST) mutations are
  not retried on an ambiguous 5xx, to avoid duplicate side effects.
- `/credentials` endpoint hardened: optional `PLAY_STORE_MCP_ADMIN_TOKEN`
  (constant-time bearer check) for deployments behind a reverse proxy; blocking
  credential validation moved off the event loop.
- Consistent error contract: read methods raise `PlayStoreClientError` instead of
  leaking raw `HttpError`, and edit transactions are always cleaned up on failure.
- List endpoints now follow `nextPageToken` — fixes silent truncation (including
  the account-access user list).
- CI: PyPI publish gated on tests/lint/type-check; least-privilege Docker workflow
  permissions; pinned `uv`.

### Fixed
- `list_app_recoveries` now sends the API-required `versionCode` (previously
  rejected).
- `__version__` is single-sourced from package metadata (was a stale `0.2.0`).
- Case-insensitive `.aab` detection.
- Subscription `start_time` / `expiry_time` populated from the v2 response.

### Security
- `pyjwt[crypto]>=2.12.0` is now a declared dependency so the CVE-2026-32597 fix
  reaches installs, not just the lockfile.
- Credential-update error responses no longer leak exception text.

### Dependencies
- Refreshed all dependencies to latest, including the majors **mypy 2.x** and
  **protobuf 7.x**. Validated: 697 tests / 100% branch coverage, ruff/mypy clean,
  pip-audit clean, and a live read-only API smoke.

## [0.3.0] - 2026-06-19

Security hardening, dependency upgrades, and CI improvements.

### Added
- Configurable DNS-rebinding protection via the
  `PLAY_STORE_MCP_DISABLE_DNS_REBINDING` environment variable (for cloud /
  reverse-proxy deployments).

### Changed
- Upgraded `mcp` 1.26.0 → 1.28.0 and `cryptography` 46.0.7 → 49.0.0.
- Hardened CI workflows, suppressed scanner false positives, and addressed
  code-review findings.

## [0.2.0] and earlier

See the [GitHub Releases](https://github.com/lusky3/play-store-mcp/releases) page.

[Unreleased]: https://github.com/lusky3/play-store-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/lusky3/play-store-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lusky3/play-store-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lusky3/play-store-mcp/compare/v0.2.0...v0.3.0
