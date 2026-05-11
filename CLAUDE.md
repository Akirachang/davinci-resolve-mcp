# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DaVinci Resolve MCP Server — provides complete coverage of the DaVinci Resolve Scripting API via MCP (Model Context Protocol). Two server modes share the same wheel of helpers under [src/utils/](src/utils/):

- **Compound** (default, 31 tools) — [src/server.py](src/server.py). Each tool dispatches on an `action` string; designed to keep agent context lean.
- **Granular** (328 tools) — [src/resolve_mcp_server.py](src/resolve_mcp_server.py), one MCP tool per Resolve scripting method. Compound entrypoint shells out to it via `python src/server.py --full`.

Plus three v2.5.0+ *authoring* compound tools (`fuse_plugin`, `dctl`, `script_plugin`) that emit and install Fusion Fuse / DCTL / Resolve-page Lua-Python source — these write to Resolve install dirs rather than calling the scripting API. v2.7.0 added a fourth non-scripting-API compound tool, `frames`, which shells out to `ffmpeg` for source-media extraction; helper lives in [src/utils/frame_extraction.py](src/utils/frame_extraction.py).

[docs/SKILL.md](docs/SKILL.md) is the canonical AI-facing reference for tool surface, page prerequisites, and known gotchas. Read it before suggesting tool calls.

## Common Commands

The release-process docs assume a `venv/` interpreter (Python 3.10–3.12; 3.13+ breaks Resolve's bridge):

```bash
# Static checks (no Resolve required)
venv/bin/python tests/test_import.py
venv/bin/python scripts/audit_api_parity.py     # API-docs-vs-source parity guard
git diff --check

# Run focused unit tests
venv/bin/python -m unittest tests.test_extract_source_frame_ranges tests.test_marker_params \
    tests.test_v232_helpers tests.test_v233_helpers tests.test_append_clip_infos_result_handling

# Run a single test method
venv/bin/python -m unittest tests.test_marker_params.TestMarkerParams.test_frame_alias

# Live Resolve validation (requires Resolve Studio running, "External scripting" = Local)
env RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting" \
    RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so" \
    PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules" \
    venv/bin/python tests/live_v233_validation.py

# Run the server directly (normally invoked by the MCP client)
python src/server.py            # compound (30 tools)
python src/server.py --full     # granular (328 tools)
```

`tests/` mixes hermetic unit tests (run anywhere) with `live_*.py` harnesses that require a running Resolve. Anything starting with `live_` will fail fast without Resolve and must use a disposable project + synthetic media — never modify, transcode, proxy, or create derivatives of real source media.

## Architecture

**Compound dispatch pattern** — every compound tool is a single `@mcp.tool()` Python function whose first arg is `action: str` and whose body is one big `if/elif` switch. Add new behavior by adding an `action` branch, not a new tool. Helpers `_ok(...)` / `_err(...)` standardize the response envelope; `_check(...)` validates the Resolve handle is live.

**Granular layering** — [src/granular/](src/granular/) is split by Resolve API surface (`project.py`, `timeline.py`, `timeline_item.py`, `media_pool.py`, `folder.py`, `media_pool_item.py`, `gallery.py`, `graph.py`, `media_storage.py`, `resolve_control.py`). [src/granular/common.py](src/granular/common.py) owns the `VERSION` constant, the shared `mcp = FastMCP(...)` instance, the Resolve path bootstrap, and shared helpers; module imports inside [src/granular/__init__.py](src/granular/__init__.py) are what register tools — adding a module without listing it there means the tools never bind.

**Connection lifecycle** — both servers cache the `resolve` handle and re-validate on each call via `_is_resolve_handle_live()` (calls `GetVersion()`); a stale handle (after Resolve restart or Project Manager transition) reconnects transparently. Don't store other Resolve objects across calls — always re-fetch from the live handle.

**Sandbox path discipline** — Resolve refuses to write to OS sandbox temp dirs on macOS/Windows. All paths Resolve will write to (project export, LUT export, still export, `grab_and_export`) MUST go through `_resolve_safe_dir()`, which redirects sandbox paths to `~/Documents/resolve-stills`. Never pass `tempfile.gettempdir()` results directly to a Resolve write call.

**Plugin-install paths** — [src/utils/platform.py](src/utils/platform.py) `get_resolve_plugin_paths()` is the single source of truth for Fuses / DCTLs / Scripts install locations across macOS/Linux/Windows. The macOS Fuse path is `…/DaVinci Resolve/Fusion/Fuses/`, NOT the `Support/Fusion/Fuses/` shown in the SDK docs (Fusion's `MapPath("Fuses:")` resolves without `/Support/`).

**API parity guard** — [scripts/audit_api_parity.py](scripts/audit_api_parity.py) parses [docs/resolve_scripting_api.txt](docs/resolve_scripting_api.txt) and verifies every documented Resolve method is wrapped somewhere in `src/`, no `from api.X` broken imports remain, and undocumented method wrappers are flagged (with an allowlist for legitimate undocumented surface like Fusion compositing API and UIManager methods). Run before every release.

**Source-vs-graded media boundary** — vision-style frame extraction (the `frames` compound tool) splits along an explicit axis: source-media reads go through `ffmpeg` directly against the file on disk (fast, ungraded — for reviewing what's *in* a clip), while timeline reads go through `Project.ExportCurrentFrameAsStill` (slower, reflects color/comp — for reviewing graded output). When adding new media-inspection helpers, pick the side of this boundary deliberately and document it in the tool's docstring. `ffmpeg` is the project's only external-binary runtime dependency; gate every use behind `frame_extraction.check_ffmpeg()` so missing-binary failures return a clear install hint rather than a stack trace. `frames` is also the first tool family that returns native MCP `Image` content blocks — when `return_images=True`, results are a mixed list of dicts + `Image` blocks rather than a pure JSON envelope.

## Version Locations

Bumping the version requires updating ALL of these together (they must match):

- [src/server.py](src/server.py) → `VERSION = "x.y.z"` (compound entrypoint)
- [src/granular/common.py](src/granular/common.py) → `VERSION = "x.y.z"` (granular package; re-exported via `src/resolve_mcp_server.py`)
- [install.py](install.py) → `VERSION = "x.y.z"`
- [README.md](README.md) line 3 — version badge `badge/version-x.y.z-blue.svg`
- [README.md](README.md) — add new "What's New in vX.Y.Z" section, demote previous to `### vX.Y.Z`
- Other badges (tool count, API coverage, live-tested %) if those numbers changed
- [docs/SKILL.md](docs/SKILL.md) when tool discovery / examples / behavior changed

Note: `src/resolve_mcp_server.py` is *not* a version surface — it imports `VERSION` from `src/granular/common.py`.

## Release Checklist (MANDATORY for every version bump)

See [docs/release-process.md](docs/release-process.md) for the full procedure. The hard requirements before commit:

1. Bump `VERSION` in all source locations above (`src/server.py`, `src/granular/common.py`, `install.py`) and the README badge; verify they match.
2. Update README badge + "What's New" section + any stale tool-count/coverage badges.
3. Update `docs/SKILL.md` if tool surface or behavior changed.
4. Run static checks: `tests/test_import.py`, `scripts/audit_api_parity.py`, `git diff --check`.
5. Run focused unit tests for the changed surface.
6. Live Resolve validation for any behavior that touches the scripting API (disposable project + synthetic media). Docs-only releases skip this but the release notes must say so.
7. Conventional commit, e.g. `chore(release): bump version to 2.4.1`.

After push: create the annotated tag `vX.Y.Z`, push the tag, then `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file …` with the changelog entry. Verify with `gh release view vX.Y.Z`.

## Coding Conventions

- Python 3.10–3.12 (3.13+ breaks Resolve's scripting bridge).
- Helper functions prefixed with `_` (e.g., `_err`, `_ok`, `_check`, `_resolve_safe_dir`).
- Compound tools dispatch on an `action` string — extend with new branches, not new tools.
- Granular tools are one-method-per-tool wrappers — preserve documented Resolve parameter shapes (PascalCase keys for image-sequence imports, `{settings}` dicts for Cloud APIs, etc.).
- All temp/sandbox paths must go through `_resolve_safe_dir()`.
- Stale Resolve handles must be detected via `_is_resolve_handle_live()` and reconnected, not assumed live.
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `security:`, `refactor:`, `chore(release):`.

## Reference Docs

- [docs/SKILL.md](docs/SKILL.md) — AI skill reference for tool surface and gotchas.
- [docs/release-process.md](docs/release-process.md) — full release procedure.
- [docs/resolve_scripting_api.txt](docs/resolve_scripting_api.txt) — vendored Resolve 20 scripting README; source of truth for the parity audit.
- [docs/fuse-dctl-authoring.md](docs/fuse-dctl-authoring.md), [docs/script-plugin-authoring.md](docs/script-plugin-authoring.md) — coverage matrix and DSL spec for the v2.5.0+ authoring tools.
- [docs/workflow-integrations.md](docs/workflow-integrations.md), [docs/openfx-notes.md](docs/openfx-notes.md), [docs/lut-notes.md](docs/lut-notes.md), [docs/fusion-template-notes.md](docs/fusion-template-notes.md), [docs/dctl-notes.md](docs/dctl-notes.md), [docs/codec-plugin-notes.md](docs/codec-plugin-notes.md) — Resolve extension-system notes used to diagnose specific tool failures.
