# TikTok Downloader v1.0 Hardening Plan

## Purpose

This release does not add features. It prepares the project to become a stable,
reusable component of **Tony Learning OS**.

The work in this phase focuses on:

- **Engineering cleanup** — remove accumulated rough edges, make the build and
  release process repeatable, and make failures visible instead of silent.
- **Reusable architecture** — extract the parts that are not TikTok-specific
  (asset indexing, AI tagging, provider configuration) into modules that other
  components can import without dragging the desktop UI along.
- **Documentation** — make the repository explain itself: architecture,
  known issues, test coverage, release artifacts, and operational steps.
- **AI pipeline foundation** — a provider-agnostic model layer plus a
  deterministic, offline-testable pipeline stage, so downstream content work
  has something solid to build on.
- **Preparing the project as a component of Tony Learning OS** — stable inputs
  and outputs, documented contracts, and no hidden coupling to the desktop shell.

Nothing in this phase is allowed to break a working feature. If a change cannot
be verified, it does not ship.

## Scope

1. **Tony Dev Kit Settings Integration**
   Align the project's configuration surface with the Tony Dev Kit settings
   conventions, so settings can be managed consistently across Tony components
   instead of each project inventing its own layout.

2. **Downloader architecture improvement**
   Continue separating concerns that are currently concentrated in a single
   large backend module. The goal is a downloader core that can be driven
   headlessly, with the desktop UI as one caller among several.

3. **AI subtitle intelligence pipeline**
   Turn existing subtitle tracks into usable outputs — transcript text,
   structured segments, and content-level metadata — through the
   provider-agnostic model layer introduced in v1.4.0.

4. **Code refactoring**
   Reduce duplication, narrow module boundaries, and replace broad
   `except Exception: pass` handling with failures that are recorded and
   surfaced where a human can act on them.

5. **Documentation system**
   Consolidate architecture notes, known issues, test inventory, and release
   records into a structure that stays current, rather than one growing file.

6. **GitHub organization migration**
   Move the project under the target GitHub organization with history, tags,
   releases, and release assets intact, and with the in-app updater repointed.

7. **Release validation**
   Define and execute a repeatable validation checklist per release: offline
   test suite, packaged self-test, real-machine launch, artifact digest
   comparison against the published release, and an end-to-end smoke run.

## Development Rules

- No uncontrolled feature expansion.
- Preserve existing working functions.
- Prefer reusable modules.
- Follow Tony AI Dev engineering standards.
- All changes require clear commits.

## Current Phase

Phase 0:
Repository preparation

Status:
In Progress

## Important Constraints

Do not:

- rewrite the application
- change UI
- change business logic
- add new dependencies unless necessary
- modify existing functionality

## Repository Baseline

Recorded at the start of Phase 0, so later phases can be compared against a
fixed reference. These are measured values, not estimates.

| Item | Value |
|---|---|
| Branch created from | `Tiktok-Content-Factory` @ `fd87e4c` |
| Application version | `1.4.0` (`outputs/TikTokBatchMVP/VERSION`) |
| Released tags | `v1.0.0` … `v1.4.0` (10 tags) |
| `origin` | `bradpittwyc/Tiktokdownloader-in-DS-Harness` |
| `upstream` | `bradpittwyc/TikTokBatchMVP` (original project, unmodified) |
| Test suite | 372 tests in 23 files, all offline |
| Test command | `python -m unittest discover -s tests -t tests -p "test_*.py"` |

Application modules (`outputs/TikTokBatchMVP/`):

| Module | Lines | Role |
|---|---|---|
| `web_app.py` | 2612 | Desktop shell, `Api` backend, download/collect pipeline |
| `app.py` | 382 | Shared helpers (`clean_profile_url`, `find_chrome`) |
| `asset_index.py` | 344 | Asset indexing, file classification, metadata schema |
| `ai_tagging.py` | 166 | Provider presets, tag prompt/parse/merge |
| `profile_pagination.py` | 129 | Cursor-chain pagination invariants |
| `session_store.py` | 101 | DPAPI-encrypted session snapshots |

Known structural debt carried into this phase (details in `架构与问题.md`):

- `web_app.py` is still a single ~2600-line module holding both the UI shell and
  the pipeline logic. Scope item 2 exists to break this up.
- Several error paths still swallow exceptions rather than recording them.
  Scope item 4 covers this.
- Architecture notes and issue lists live in one large file. Scope item 5
  covers this.

## Notes On This Document

- The section headings and the item lists above follow the v1.0 hardening brief
  as given. The **Repository Baseline** section was added so the plan states the
  actual starting point instead of assuming one.
- Two scope items — *Tony Dev Kit Settings Integration* and
  *GitHub organization migration* — reference infrastructure outside this
  repository. They are recorded here as planned work; their concrete acceptance
  criteria still need to be supplied before they can be executed.
