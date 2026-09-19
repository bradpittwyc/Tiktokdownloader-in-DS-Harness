# Tony Dev Kit Reusable Asset Audit

Audit only. **No code was modified** in either repository.

| | |
|---|---|
| Repository A | `bradpittwyc/Tiktokdownloader-in-DS-Harness` — TikTok Downloader, Python + pywebview |
| Repository B | `Tony-AI-Dev/tony-dev-kit` — read-only clone at `%TEMP%\tony-devkit-audit`, HEAD `7c6ae53` ("Create first Tony App from template", 2026-09-18) |
| Method | Read-only inspection of both trees; no builds, no installs, no writes |
| Date | Recorded at Phase 2.3.1 |

---

## 0. Headline Finding

**The Dev Kit is a TypeScript/React project. TikTok Downloader is a Python/pywebview
application. Nothing in the Dev Kit is importable by Python.** 67 `.ts` + 36 `.tsx` files,
zero `.py`, zero `requirements.txt` / `pyproject.toml`.

But a more important finding came out of the audit:

> **A sibling Python repository already implements the settings and credential
> foundation, in Python, and is a direct descendant of TikTok Downloader.**
>
> `bradpittwyc/Tony-Content-Factory` (private, last pushed 2026-09-17).
> It contains `outputs/TikTokBatchMVP/content_factory/settings/` (schema, defaults,
> validators, migration, service) and `outputs/TikTokBatchMVP/content_factory/providers/`
> (`credentials.py`, 805 lines). See §1.3 and §6.

The Phase 2.3.1 brief asked for a from-scratch `settings/{schema,adapter,credential_store}.py`.
That would have **duplicated an implementation that already exists in Python, is further
along, and is already deployed** — the exact thing this audit exists to prevent. That
work was rolled back before this audit began; this document records why it should stay
rolled back in favour of reuse.

---

## 1. Repository Structure

### 1.1 Repository B — `tony-dev-kit`

| Concern | Location | Notes |
|---|---|---|
| Settings standard | `standards/SETTINGS_STANDARD.md` (212 lines) | Six fixed modules, two-column layout, no re-design allowed |
| AI runtime standard | `docs/AI_RUNTIME_STANDARD.md` (362 lines) | No app calls a model API directly; single retry layer |
| UI standard | `docs/UI_COMPONENT_STANDARD.md` (430 lines) | Reuse, never copy components into an app |
| Settings schema (app side) | `templates/tony-app-template/src/settings/schema.ts:43-60` | Hand-written TS interfaces, no zod |
| Settings storage port | `templates/.../src/settings/store.ts:27-47` | `load/save/clear`, all async |
| Credential store port | `templates/.../src/settings/store.ts:53-75` | `has/set/remove/references` — **no read method** |
| Settings UI | `components/settings/` (13 files + 5 modules) | React only |
| Secret input | `components/ui/secret-input.tsx` (165 lines) | Write-only password field |
| AI provider modules | `ai-runtime/providers/` (17 files) | Declarative descriptors |
| AI runtime core | `ai-runtime/core/` (9 files) | `runtime.ts`, `config.ts`, `model-catalog.ts`, … |
| Shared utilities | `lib/` (`cn.ts`, `variants.ts`, `use-controllable-state.ts`) | React-only |
| Design tokens | `styles/theme.css` (130 lines), `tailwind-preset.ts` | Tailwind v3 artifacts |

### 1.2 Repository A — TikTok Downloader

| Concern | Location | Notes |
|---|---|---|
| Backend + UI shell | `outputs/TikTokBatchMVP/web_app.py` (2612 lines) | Single module, `Api` class |
| AI provider presets | `outputs/TikTokBatchMVP/ai_tagging.py:30-40` | 3 presets (OpenAI / DeepSeek / custom) |
| Model call | `web_app.py` → `_call_model()` | One OpenAI-compatible POST |
| Credential handling | `outputs/TikTokBatchMVP/session_store.py:42-62` | `dpapi()` + `SessionStore` |
| GitHub token | `web_app.py` → `github-token.dpapi` | DPAPI file |
| Settings (scattered) | `learning.json`, `filename-template.txt`, localStorage `tiktok-prefs` | **Three different mechanisms** |
| **Secret in a settings file** | `learning.json` → `api_key` | Plaintext; confirmed non-empty on the audit machine |

### 1.3 Repository C — `Tony-Content-Factory` (found during this audit)

Private repo, same owner. A descendant of TikTokBatchMVP — it still carries
`outputs/TikTokBatchMVP/web_app.py` (2332 lines), `session_store.py`, `app.py`,
`profile_pagination.py`.

| Concern | Location | Size |
|---|---|---|
| Settings schema | `content_factory/settings/schema.py` | 212 lines, `SCHEMA_VERSION = 2` |
| Settings defaults | `content_factory/settings/defaults.py` | 425 lines |
| Settings validation | `content_factory/settings/validators.py` | 341 lines |
| Settings migration | `content_factory/settings/migration.py` | 136 lines |
| Settings service | `content_factory/settings/service.py` | 611 lines |
| Credentials | `content_factory/providers/credentials.py` | 805 lines |
| Provider registry | `content_factory/providers/registry.py` | 262 lines |
| Provider models | `content_factory/providers/models.py` | 611 lines |
| Connection test | `content_factory/providers/connection_test.py` | 404 lines |
| Settings adapter | `content_factory/providers/settings_adapter.py` | 416 lines |

**Verified equivalence**: running `defaults()` from that checkout produced
**13 sections / 205 fields**; the live settings document on this machine has
**14 top-level keys / 206 fields** (13 sections + `schema_version`). They match exactly —
this is the application that owns the `content-factory-*` files in
`%LOCALAPPDATA%\TikTokBatchMVP`.

---

## 2. Reusable Components

`Tech` = technology of the component, not of the consumer.

| Component | Location | Tech | Reusable? | Integration Method |
|---|---|---|---|---|
| SETTINGS_STANDARD (6 fixed modules, 2-column layout, reuse rules) | `standards/SETTINGS_STANDARD.md` | Markdown spec | **Yes — as a spec** | Copy pattern only (translate to Python/HTML constraints) |
| AI_RUNTIME_STANDARD (no direct API calls; env *reference* not value; single retry layer; error `code` branching) | `docs/AI_RUNTIME_STANDARD.md` | Markdown spec | **Yes — as a spec** | Copy pattern only |
| UI_COMPONENT_STANDARD | `docs/UI_COMPONENT_STANDARD.md` | Markdown spec | Partly | Copy pattern only (design-token naming; component layering) |
| Provider metadata: 17 × `{id, label, defaultBaseURL, defaultApiKeyEnv}` | `ai-runtime/providers/*.ts` | TS constants | **Yes — the data** | Copy pattern only (transcribe to a Python table) |
| Model catalog: 36 entries `{id, label, contextWindow, maxTokens}` | `ai-runtime/core/model-catalog.ts:50-114` | TS constants | **Yes — the data** | Copy pattern only (transcribe) |
| Credential *discipline*: reference-not-value, write-only API, no read-back, blank = unchanged, per-request resolve | `components/ui/secret-input.tsx`, `ai-runtime/core/config.ts:339-380` | Design rule | **Yes — the model** | Copy pattern only |
| `normalizeSettings` semantics (repair-not-reject, issue list, numeric bounds) | `templates/.../src/settings/schema.ts:131-176` | TS function | Semantics yes, code no | Copy pattern only |
| `SecretInput` component | `components/ui/secret-input.tsx` | React | **No** | — (rewrite for pywebview) |
| `SettingsLayout` / `SettingsSidebar` / `SettingsPanel` / `SettingsSection` / `SettingsItem` / `SettingsForm` | `components/settings/` | React + Tailwind | **No** — and the standard forbids copying them | — |
| `lib/cn.ts`, `lib/variants.ts`, `lib/use-controllable-state.ts` | `lib/` | React/JS | **No** | — |
| `styles/theme.css`, `tailwind-preset.ts` | root | Tailwind v3 | Token names only | Copy pattern only |
| `ai-runtime` runtime (`TonyAI.chat/stream`) | `ai-runtime/core/runtime.ts` | TS, needs Vercel AI SDK | **No** | Not callable from Python; no HTTP/CLI/stdio surface exists |
| `scripts/scan-encoding.mjs` (mojibake scanner) | `scripts/` | Node ESM | **Yes — concept** | Package dependency (optional) or reimplement in CI |
| **`content_factory/settings/`** | `Tony-Content-Factory` | **Python** | **Yes — directly** | **Package dependency / direct import** |
| **`content_factory/providers/credentials.py`** | `Tony-Content-Factory` | **Python + DPAPI** | **Yes — directly** | **Package dependency / direct import** |
| **`content_factory/providers/registry.py` + `models.py`** | `Tony-Content-Factory` | **Python** | **Yes — directly** | **Package dependency / direct import** |

---

## 3. Settings System

### 3.1 In the Dev Kit (Repository B)

| Layer | Finding |
|---|---|
| Schema | Hand-written TS interfaces (`templates/.../src/settings/schema.ts:43-60`). **No zod.** **No `schemaVersion`. No migration.** Seven top-level sections: `general`, `appearance`, `storage`, `providers`, `defaultProvider?`, `requestDefaults`, `account?` |
| Storage | **Browser `localStorage` only** — `tony-app:settings` (`templates/.../src/services/settings-store.ts:28`). No file, no Tauri, no IndexedDB, no HTTP. **No atomic write.** The port is async by design (`store.ts:40-46`) |
| Validation | `normalizeSettings(raw)` → `{settings, issues}` (`schema.ts:131-176`). **Repairs rather than rejects.** Bounds: `fontSize` 12–20, `temperature` 0–2, `maxTokens` 1–200000, `timeoutMs` 1000–600000, `maxRetries` 0–10. Cross-field: `defaultProvider` must reference a configured provider or is cleared |
| UI layer | `components/settings/` — React + Tailwind, `'use client'`, hooks. **React only.** No headless/vanilla/JSON-driven renderer, no standalone CSS/JS artifact |
| Backend layer | **Does not exist.** No Tauri command, no IPC, no HTTP settings endpoint. `templates/.../src/main.ts:31-38` only carries a comment about swapping the adapter |

### 3.2 In TikTok Downloader (Repository A)

Settings are split across three unrelated mechanisms with no shared schema and no
validation layer:

| What | Where | Format |
|---|---|---|
| UI preferences | WebView `localStorage` key `tiktok-prefs` | JSON |
| File name template | `%LOCALAPPDATA%\TikTokBatchMVP\filename-template.txt` | Plain text |
| AI config **+ API key** | `%LOCALAPPDATA%\TikTokBatchMVP\learning.json` | JSON, **key in plaintext** |
| TikTok session | `tiktok-session.dpapi` | DPAPI |
| GitHub token | `github-token.dpapi` | DPAPI |

### 3.3 Can TikTok Downloader directly consume the Dev Kit's settings system?

**No — at any layer.**

- The schema is TypeScript. Python cannot import it.
- Storage is `localStorage` inside the webview; the Python backend cannot read it, and
  it is the wrong place for settings that the backend must act on (download folder,
  concurrency, quality).
- The UI is React. TikTok Downloader's front end is a single vanilla `index.html`
  with inline JS; adopting the components would mean introducing React + Tailwind + Vite
  into a pywebview app — a stack change, not an integration.
- There is no backend layer to attach to.

**The Dev Kit's settings system is reusable as a *specification*, not as code.** Its
port signatures (`load/save/clear`, `has/set/remove/references`) and its
repair-not-reject validation semantics are worth adopting verbatim.

---

## 4. AI Provider System

### 4.1 Dev Kit — `ai-runtime/` (17 providers)

Declarative, not adapter-per-provider. A provider is metadata plus one `create()` closure
(`ai-runtime/core/provider.ts:61-99`); the layer contains **no HTTP, no header assembly,
no SSE parsing** (`ai-runtime/providers/index.ts:21-47`).

| Capability | Status |
|---|---|
| OpenAI-compatible API | **Yes** — `createCompatibleModel` (`ai-runtime/core/provider-support.ts:197-211`) covers 8 providers |
| DeepSeek | **Yes** — `ai-runtime/providers/deepseek.ts:18-32`, `https://api.deepseek.com`, `deepseek-chat`, env `DEEPSEEK_API_KEY` |
| Ollama | **Yes** — `ai-runtime/providers/ollama.ts:27-39`, `http://localhost:11434/v1`, `llama3.2`, **no credential required** |
| Other local runtimes | vLLM (`:8000/v1`), llama.cpp (`:8080/v1`) — also credential-free |
| Model switching | **Yes, per request** — `model` on `ChatRequest`; resolution order request → `profile.defaultModel` → `profile.models[0]` (`ai-runtime/core/config.ts:320-329`). No `setModel()`; changing the default needs `configure()` |
| Temperature | **Yes** — request overrides config (`ai-runtime/core/runtime.ts:439-440`), range 0–2 |
| Other request parameters | **`maxTokens`** (clamped by `min(request, config)`), **`system`**, **`maxRetries`**, **`timeoutMs`/abortSignal**. **`top_p`, `frequency_penalty`, `presence_penalty`, `seed`, `stop` do not exist anywhere in the repository** |
| Streaming | Yes — `async *stream()` → `AsyncIterable<ChatChunk>` (`runtime.ts:231`) |
| Model discovery | Partial. The catalog is a **static list of 36 entries** (`model-catalog.ts:50-114`). Six providers implement `listModels`, but `TonyAI` **exposes no `discoverModels`** — the template itself notes this gap (`templates/.../src/services/ai.ts:253-272`) |
| Fallback / caching | None (`docs/AI_RUNTIME_STANDARD.md:252-258`) |

### 4.2 TikTok Downloader

Three hard-coded presets in `ai_tagging.py:30-40` (OpenAI / DeepSeek / custom), one
OpenAI-compatible POST in `web_app.py::_call_model()`, parameters limited to
`temperature` and `max_tokens`. Providers are a Python dict, which is already the same
shape as the Dev Kit's descriptor data.

### 4.3 Verdict

The Dev Kit's **provider table is data and transcribes cleanly to Python** (17 base URLs,
17 env-var names, 36 catalog entries, and the descriptor field names). Its **runtime is
not reachable from Python**: `TonyAI` is an in-process TypeScript library with no HTTP,
CLI, or stdio surface, it is `private: true` with no build output (`package.json` has no
`main`/`types`/`bin`, and `exports` points straight at `.ts` source), and it depends on
the Vercel AI SDK family (`ai` + 10 `@ai-sdk/*` packages).

---

## 5. Credential Management

### 5.1 Dev Kit

| Aspect | Finding |
|---|---|
| API key storage | A separate `CredentialStore` port — **not** part of the settings document. `templates/.../src/settings/store.ts:58-61`: *"A key is not a setting… belongs in a keychain, not in the settings document."* The port is `has/set/remove/references` — **there is no read method** (`store.ts:76-94`) |
| Encryption | **None anywhere in the repository.** Searches for `crypto`/`encrypt`/`decrypt`/`cipher`/`base64`/`atob`, and for `keychain`/`keyring`/`keytar`/`safeStorage`, return zero hits. The only shipped implementation is `localStorage` under `tony-app:credential:` (`templates/.../src/services/settings-store.ts:123-158`), and `README.md:164-171` concedes it is unsuitable for deployed products |
| Environment variables | Convention is Node `process.env`, resolved through `environmentCredentialResolver` (`ai-runtime/core/config.ts:48-78`); 14 providers declare a `defaultApiKeyEnv`. The standard states explicitly that environment is a **development fallback, not a storage mechanism** (`docs/AI_RUNTIME_STANDARD.md:185-188`). No `import.meta.env`, no `VITE_` prefix, no `.env` file in the repository |
| Local storage | `localStorage`, per the implementation above |
| Format validation | `normalizeCredential()` rejects `NAME=value` lines, surrounding quotes, and non-printable ASCII (`ai-runtime/core/config.ts:86-112`) |
| Leak prevention | Read APIs answer configured/not-configured only; headers containing `authorization`/`api-key`/`secret`/`token`/`cookie` fail validation (`config.ts:175-188`); user-facing errors never echo the raw message |

**This is the Dev Kit's strongest asset for TikTok Downloader**, and it is purely a
discipline: *reference, not value; write-only; never read back; blank means unchanged;
resolve per request; never log.*

### 5.2 TikTok Downloader — current state

- **A real API key is stored in plaintext** in `learning.json` (verified on this machine:
  the `api_key` field is present and non-empty). This violates the Dev Kit's rule and is
  the one concrete security gap this audit found on the TikTok side.
- DPAPI is already used correctly for the TikTok session (`session_store.py:42-62`) and
  the GitHub token.
- `web_app.py::_github_headers()` already follows a partial version of the "reference,
  not value" idea.

### 5.3 The two are reconcilable

The Dev Kit requires *file separation and interface separation*. DPAPI ciphertext sitting
in the same document as ordinary settings would still violate it. Moving the AI key into
its own DPAPI file, behind a store with no read-back API, satisfies both the Dev Kit's
rule and the existing DPAPI approach.

---

## 6. Integration Recommendation

**Do not design a framework. Do not port the Dev Kit to Python.** The smallest correct
path has three steps, in this order.

### Step 1 — Reuse the Python sibling (highest value, lowest risk)

`Tony-Content-Factory` already contains, in Python:

- `content_factory/settings/` — schema, defaults, validators, migration, service
- `content_factory/providers/credentials.py` — `CredentialStore`, four backends
  (`dpapi` / `test` / `plain` / `none` selected by `CONTENT_FACTORY_CREDENTIAL_BACKEND`),
  plus `is_secret_name`, `mask_secret`, `redact`, `redact_mapping`,
  `SecretRedactionFilter`, `install_log_filter`
- `content_factory/providers/registry.py`, `models.py`, `connection_test.py`,
  `settings_adapter.py`

These are directly importable by TikTok Downloader — same language, same
`%LOCALAPPDATA%\TikTokBatchMVP` data root, and the credential module is a strict
superset of the Dev Kit's discipline (it adds redaction and a test backend, which the
Dev Kit has no equivalent for).

**Integration method:** package dependency or vendored import. Before doing so, extract
the shared part into something both apps depend on rather than having TikTok Downloader
reach into the other application's tree.

### Step 2 — Adopt the Dev Kit's standards as written rules

Copy `standards/SETTINGS_STANDARD.md` and `docs/AI_RUNTIME_STANDARD.md` into this
repository's `standards/` and follow them where they apply to a Python app:

- a single settings page, fixed section order, no per-project re-design
- no component of the settings surface may be copied between projects
- API keys are references, never values; the credential store has no read-back API
- exactly one retry layer
- branch on error `code`, never on the message

Item 2 is a **constraint on this repository**, not a task: it means the vanilla
`index.html` settings modal must not be presented as a reusable component.

### Step 3 — Transcribe the provider data

Translate the 17 provider descriptors (base URL + key env var) and the 36-entry model
catalog into a Python constant table. This is a data transcription, not an
implementation — `ai_tagging.PROVIDERS` is already the same shape.

### Explicitly not recommended

| Option | Why not |
|---|---|
| Node sidecar wrapping `TonyAI` | The Dev Kit ships zero support for it (no HTTP/CLI/stdio surface). It would mean designing and owning a new protocol, plus shipping Node ≥20 to users |
| Adopting React + Tailwind + Vite in the webview | A stack replacement, not an integration. The standard also forbids copying the components into an app, so this would not even reduce the work |
| Re-porting the Dev Kit's settings/crypto layer to Python by hand | **Already done, in Python, in `Tony-Content-Factory`.** This is exactly what Phase 2.3.1 proposed before it was rolled back |

---

## 7. Open Items And Risks

| # | Item | Impact |
|---|---|---|
| 1 | `content_factory/settings/schema.py:62` declares `SCHEMA_VERSION = 2`, and `migration.py` only registers `_v1_to_v2` — but the **live settings document is `schema_version: 3`** | The local checkout at `E:\App construction\Content Factory\Tony-Content-Factory` is **behind the data it owns**. Reconcile before any migration work |
| 2 | `Tony-Content-Factory` is **private** and lives outside this workspace | Reuse requires either access or extracting the shared part into a third package. Decide the ownership model before depending on it |
| 3 | Both applications write into `%LOCALAPPDATA%\TikTokBatchMVP` | Namespace collision. Two applications sharing one data directory will eventually fight over `settings.json` / `credentials.json` |
| 4 | Dev Kit has no `schemaVersion` and no migration | Any settings contract adopted from it must add versioning itself — the Dev Kit will not supply it |
| 5 | Dev Kit ships no encryption | Correct for a browser app; not sufficient for a desktop app that already has DPAPI available |
| 6 | Dev Kit `ai-runtime` has no `top_p` / penalty / seed / `stop` | If TikTok Downloader ever needs those, they are not "coming from the kit" — they must be added locally |
| 7 | `.editorconfig` in the Dev Kit is a 0-byte file | It imposes nothing; do not cite it as a convention source |

## 8. What Was Not Verified

- The Dev Kit was never built or run. `typecheck` and its test suite were not executed;
  the README's claim of "68 tests passing" was not reproduced.
- The three `@ai-sdk/*`-based providers were read, not exercised.
- `Tony-Content-Factory` was inspected read-only from a local checkout. Its test suite
  (including `tests/test_settings_core.py`, 797 lines, and `tests/test_provider_config.py`,
  1312 lines) was **not** run.
- The provider count is 17 as declared by `ai-runtime/providers/index.ts:48-69` and
  asserted by `ai-runtime/__tests__/runtime.test.ts:46-55`. An earlier working note in
  this session said 18; 17 is the verified number.
