# AGENTS.md

This file provides guidance for AI coding agents working in the `dsh-plugin-bundle` repository.

## Project Overview

`dsh-plugin-bundle` is a plugin and companion tooling bundle for **DeepSeek Harness (DSH)** that integrates Google Antigravity (`agy`) as a primary LLM provider.

The repository consists of two independent top-level directories:

| Directory | Purpose | Distribution |
|---|---|---|
| `dsh-llm-antigravity-acp/` | Primary DSH plugin (`llm-antigravity-acp`) driving local `agy acp --process-per-session` over ACP v1 with a loopback HTTP MCP bridge for DSH tools, quota UI, and `node:sqlite` step usage tracking. | Installed as a DSH plugin (`dsh plugin add ...#main&path:/dsh-llm-antigravity-acp`). |
| `agy-context-unlock/` | Optional standalone Python utilities that patch local official Antigravity binaries (`agy` CLI and `language_server`) to unlock the 512K/1M context window, remove 64K per-step truncation, and bypass region/eligibility gates. | **Not** bundled into the DSH plugin package; manually deployed to `~/.gemini/antigravity/tools/`. |

## Repository Layout

### `dsh-llm-antigravity-acp/`
- `src/` — TypeScript source (`*.ts`) and uncompiled browser UI module (`src/client.js`).
  - `index.ts` — Cordis plugin entry (`name = 'llm-antigravity-acp'`, `inject = ['llm', 'subprocess']`).
  - `config.ts` — Schemastery configuration schema; `contextWindow`, `defaultEffort`, and `longDecodeContinuations` are marked `.volatile()` for live Web UI updates.
  - `adapter.ts` / `runtime.ts` — `AntigravityAdapter` & `AntigravityRuntime`: manages per-`(sessionId, purpose)` ACP worker processes, tool-call parking/relay (`relayAfterMs` keepalive before agy's 180s MCP timeout), subagent concurrency, and long-decode continuations.
  - `acp-process.ts` / `jsonrpc.ts` — Subprocess spawning and JSON-RPC over stdio transport.
  - `mcp-bridge.ts` — Per-session `127.0.0.1` HTTP MCP bridge exposing DSH tools as `mcp_dsh_<name>`, plus `dsh_call_tool` and `dsh_await_result` to preserve prompt-prefix caching.
  - `conversation.ts` / `history.ts` — DSH-to-ACP message mapping and byte-budgeted history replay (`2.2MB` unlocked, `160KB` locked).
  - `usage-db.ts` — Read-only `node:sqlite` reader for `$home/agy/conversations/<sessionId>.db` (`steps` table, `step_type = 15` Protobuf `ModelUsageStats`).
  - `catalog.ts` — Model capabilities and `HIDDEN_MODEL` filter (Claude Sonnet 5.5 and GPT-OSS 120B are hidden from the picker by default).
  - `preflight.ts` / `quota.ts` / `remote.ts` — Binary discovery, optional `autoPatch` invocation, `agy -p /usage` quota polling, and DSH remote RPC registration.
  - `client.js` — Browser module loaded via `window.__ModuleLoader__.load`; renders the composer quota pill and settings card.
- `lib/` — Pre-compiled JavaScript output (`*.js`). **Committed to Git** so `dsh plugin add "git+..."` works without requiring a build step on the target machine.
- `scripts/build.mjs` — Custom build & typecheck script linking peer packages from a DSH checkout, compiling `src/*.ts` -> `lib/*.js`, and copying `src/client.js` -> `lib/client.js`.
- `tests/` — `node:test` test suite (`fake-agy.mjs`, `runtime.test.mjs`, `client-quota-ui.test.mjs`).
- `cordis.patch.yml` — DSH bundle mount declaration.
- `locale/` — `en.json` and `zh.json` translations.

### `agy-context-unlock/`
- `agy-patch.py` — Multi-platform (Mach-O, ELF, PE; arm64 & x86-64) Go `pclntab` binary patcher for Gate 1 (`offersContextWindow`), Gate 2 (`Store.ContextWindow`), Gate 3A/3B (checkpoint ceiling), and Gate 4A/4B (64K `USER_INPUT` step clamp).
- `more-region.py` — Windows x64 `agy.exe` region/eligibility patch (`IneligibilityFromResult`).
- `agy-desktop-region-patch.py` — Windows x64 GUI `language_server.exe` region/eligibility patch (`ValidateAndOnboardAccount`).
- `agy-launcher.pyw` / `antigravity-launcher.pyw` — Windows zero-window wrappers that run fast `patch-stamp.json` checks / `--in-place` patching before launching CLI or Desktop binaries.
- `agy-autopatch.py` — macOS `launchd` auto-repatch daemon.

> **Architecture & Patch Boundaries (`agy-context-unlock/`)**:
> - **CLI vs. Desktop targets**: `more-region.py` patches CLI-only `agy.exe` (`google3/third_party/jetski/cli/backend/backend.IneligibilityFromResult`), which does not exist in `language_server.exe`. Desktop GUI (`Antigravity.exe`) serves its Hub UI (`/main.js`) from `language_server.exe`'s local HTTPS server and enforces login eligibility via `GetAuthStatus` -> `SET_INELIGIBLE`; `agy-desktop-region-patch.py` targets `google3/third_party/jetski/language_server/auth_client/authclient.ValidateAndOnboardAccount` in `language_server.exe` (redirecting post-prologue to the internal `hasValidAuth = 1, failureDetails = nil, err = nil` epilogue `c6400801c640200031db31c9c9c3`).
> - **Client gates vs. API geo-checks**: Binary patches bypass local CLI/LS gates only. Live LLM requests still hit Google ESF (indicated by `X-Cloudaicompanion-Trace-Id` / `400 FAILED_PRECONDITION: User location is not supported for the API use.`) if egress traffic bypasses a supported proxy region. Core endpoints used by Antigravity include `daily-cloudcode-pa.googleapis.com`, `cloudcode-pa.googleapis.com`, `generativelanguage.googleapis.com`, `aicode.googleapis.com`, `cloudaicompanion.googleapis.com`, `aiplatform.googleapis.com`, `businessaicode.googleapis.com`, `oauth2.googleapis.com`, and `auth.cloud.google`.
> - **Windows deployment mapping**: Active Windows wrappers run from `~/.gemini/antigravity/tools/` (`more-region.py` is deployed there as `agy-region-patch.py`; other scripts keep their repository filename). When updating repo scripts, remind or sync deployed copies accordingly.
> - **Automation & Trigger Flows**:
>   - *Windows CLI*: PowerShell Profiles (`function agy`) invoke `agy-launcher.pyw --check-only` both before `agy.exe` and after `agy update`; `agy-launcher.pyw` compares `size`/`mtime_ns` against `patch-stamp.json` and runs `agy-patch.py` (`512k`) then `agy-region-patch.py --apply` when changed.
>   - *Windows Desktop GUI*: Launch shortcut (`Antigravity.lnk`) runs `pythonw.exe .../antigravity-launcher.pyw`, which patches `language_server.exe` (`agy-patch.py` + `agy-desktop-region-patch.py --apply`) *before* spawning `Antigravity.exe` to avoid runtime file locks (in-app Electron auto-updates require a full quit and relaunch via the launcher).
>   - *DSH Plugin*: `dsh-llm-antigravity-acp` preflight (`autoPatch: true`) runs `patchScript` (`agy-patch.py`) for context unlock only, not region patchers.
>   - *macOS*: `agy-autopatch.py` via `launchd` watches `~/.local/bin` and `/Applications/Antigravity.app`, re-patching vanilla binaries and signaling `SIGTERM` to `language_server`.

## Setup & Prerequisites

- **Node.js**: `>=22.19` (required for `node:sqlite` and native ESM features).
- **DeepSeek Harness (DSH) Checkout**: `dsh-llm-antigravity-acp` has peer dependencies (`@deepseek-ai/cordis`, `@deepseek-ai/dsh-llm`, `@deepseek-ai/dsh-subprocess`, `@deepseek-ai/dsh-typert-protocol`, `@deepseek-ai/schemastery`) and uses TypeScript from the host checkout.
  - `scripts/build.mjs` automatically discovers the DSH checkout via:
    1. `DSH_CHECKOUT` environment variable,
    2. ancestor directories carrying `packages/llm/llm/package.json`, or
    3. `link:` dependencies inside `$DSH_HOME/profiles/*/package.json` (defaults to `~/.dsh/profiles`).
  - Running `npm run build` or `npm run typecheck` creates symlinks in `dsh-llm-antigravity-acp/node_modules/` pointing to that DSH checkout.
- **Python 3**: Required only when running or testing scripts in `agy-context-unlock/`. All scripts use the Python standard library exclusively (no `pip` packages needed).

## Build & Development Commands

Run inside `dsh-llm-antigravity-acp/`:

```sh
# Type-check only (no emit)
npm run typecheck

# Compile src/*.ts -> lib/*.js and sync src/client.js -> lib/client.js
npm run build
```

> **Important**: Whenever you edit any file in `dsh-llm-antigravity-acp/src/` (including `src/client.js`), you **must** run `npm run build` before finishing so that `lib/` stays in sync in Git.

## Testing Instructions

Run inside `dsh-llm-antigravity-acp/`:

```sh
# Run the full test suite (requires DSH peer symlinks in node_modules/ for runtime tests)
npm test

# Run standalone UI & client.js sync tests (works even without a DSH checkout)
node --test tests/client-quota-ui.test.mjs

# Run runtime ACP/MCP integration tests (requires DSH_CHECKOUT / linked node_modules)
node --test tests/runtime.test.mjs
```

### Testing `agy-context-unlock/` Scripts

Always run binary patchers in read-only `--dry-run` (or default dry-run) mode before applying changes:

```sh
# Context unlock dry-run check
python agy-context-unlock/agy-patch.py --binary <path-to-agy-or-language_server> --dry-run

# Region unlock dry-run checks (Windows x64)
python agy-context-unlock/more-region.py
python agy-context-unlock/agy-desktop-region-patch.py
```

Note on Windows:
- Running `language_server.exe` holds an exclusive file lock (`PermissionError` / `Errno 13`); stop the Antigravity Desktop app before testing `--apply` on it (`antigravity-launcher.pyw` runs context + region patches prior to spawning `Antigravity.exe` for this reason).
- Set `PYTHONUTF8=1` when running Python checks in PowerShell, and put multi-line inspection scripts in temporary files instead of inline `python -c "..."` to avoid `pwsh` quoting/brace errors.

## Code Style & Conventions

### TypeScript & JavaScript (`dsh-llm-antigravity-acp/`)
- **TypeScript Config**: Strict mode is enforced (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, `noFallthroughCasesInSwitch`, `noUnusedLocals`, `noUnusedParameters`).
- **ESM & Imports**: `module: "nodenext"`. Use explicit `.ts` extensions in relative imports inside `src/*.ts` (`allowImportingTsExtensions` and `rewriteRelativeImportExtensions` are enabled). Prefer `node:` prefix for Node built-ins (e.g., `node:fs`, `node:sqlite`, `node:test`).
- **Formatting**: 2-space indentation, single quotes, no semicolons, trailing commas in multiline literals/arguments.
- **Browser Module (`src/client.js`)**: Written in plain ES module-loader format (`window.__ModuleLoader__.load(...)`) using `React.createElement` directly without JSX/bundling. Must remain byte-for-byte identical to `lib/client.js` (enforced by `tests/client-quota-ui.test.mjs`).
- **Documentation**: Use concise JSDoc comments in English for modules, exported types, config fields, and functions.

### Python (`agy-context-unlock/`)
- Standard library only — do not add third-party dependencies so scripts can run directly via system Python or launcher hooks.
- All binary patches must dynamically locate Go symbols via `pclntab` (avoid hardcoded file offsets across binary builds) and must be idempotent (`already patched` on repeated runs).
- **PE x86-64 `pclntab` & codegen variants**:
  - Standalone `pclntab` scanners (like `more-region.py` and `agy-desktop-region-patch.py`) see full `google3/...` symbol paths unless normalized (unlike `agy-patch.py`'s `build_functions()`).
  - Gate 4A (`_x86_find_epilogue`) must accept both `add $imm,%rsp ; pop %rbp ; ret` (`5d c3`) and `leave ; ret` (`c9 c3`).
  - Gate 4B (`patch_gate4b_oneharness_user_input`) must preserve both the forward `cmp $0xe ; je` pattern and the inverted Windows PE pattern (`cmp $0xe ; jne` falling through to the 64K load, patched in-place with `xor` + `nop` padding).
- Support a non-destructive read-only check mode (`--dry-run` or default dry-run) and create `.bak` backups on explicit write unless `--no-backup` is passed.

### Git & Documentation
- `README.md` files and commit messages in this repository are primarily written in Chinese.
- Never commit `node_modules/`, `*.tsbuildinfo`, or `__pycache__/`.
- Keep `lib/` artifacts updated whenever `src/` changes.
