# Changelog

Notable changes to dsh-fusion360. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `examples/build_a_part.py` — a commented 100 × 60 × 8 mm plate with R6 corner
  rounds and four ⌀4.5 through holes, self-checking against the closed-form
  volume at each stage.
- `docs/install.md`, `docs/verification.md`, `docs/fusion-api-notes.md` — the
  README's install, measurement, and API-drift material, expanded.
- `README.zh-CN.md` — Chinese translation of the README.
- `docs/dsh-compatibility.md` — what this project depends on inside DSH, what was
  re-checked for DSH Desktop 2.0.17, and the commands to re-run the audit after
  the next update.
- `package.json` — declares `dsh.bundle.patch`, which lets plugin catalogs
  identify this repository. Marked `"private": true`: the install is two-part and
  npm cannot complete it.
- `mcp_server/fusion_mcp_server.py` now returns an `instructions` string from
  `initialize`. DSH appends it to the agent's system prompt, so the 13 tools
  arrive with the fact that Fusion has to be running and that the API is in
  centimetres.

### Fixed

- `examples/build_a_part.py` claimed it ran unmodified inside Fusion's own Python
  console. It did not: `design` is only pre-bound by `fusion_run_python`, so the
  file raised `NameError` anywhere else. It now resolves `app` and `design`
  itself and was re-tested with **no** pre-bound globals, reproducing
  48000.00 / 47752.78 / 47243.84 unchanged.
- `FusionDSHBridge.manifest` reported version `1.0.0` while the code inside it was
  `1.0.3`, so Fusion's Add-Ins dialog disagreed with `fusion_status`. Both now
  say `1.0.3`.
- `mcp_server/fusion_mcp_server.py` pointed its protocol-version comment at
  `@modelcontextprotocol/sdk` 1.30.0, which DSH no longer ships. The list itself
  was already correct and is now documented against
  `@modelcontextprotocol/core` 2.0.0.

### Changed

- `tools/restart_dsh.py` locates DSH Desktop instead of hard-coding a path: it
  asks a running instance, then falls back to conventional install locations, and
  honours `DSH_DESKTOP_EXE`.
- `tools/install_dsh_mcp.py` — idempotent registration of the MCP server into a
  DSH profile, with a timestamped backup before any write.
- The `.ps1` and `.bat` files under `tools/` are stored normalised and checked out
  CRLF, as `.gitattributes` always said they were.

## [1.0.3] — bridge

### Changed

- `FusionDSHBridge.py` — bridge version **1.0.3**. Adds `reload`, so the add-in
  can re-execute itself from disk and rebind its listener without a trip through
  Fusion's Scripts and Add-Ins dialog.

### Fixed

- `_log` obtains the `Application` proxy inside the calling thread. A proxy
  captured on the primary thread does not marshal across.
- Event handlers are held at module level. Fusion's event plumbing keeps weak
  references, so a handler referenced only from the add-in instance is collected
  and the bridge goes deaf with no error.

## [1.0.0]

### Added

- Initial release: the Fusion 360 add-in, the standard-library MCP server, the
  DSH profile patch, and the three test harnesses.

[Unreleased]: https://github.com/Leaveing00001/dsh-fusion360/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/Leaveing00001/dsh-fusion360/releases/tag/v1.0.3
[1.0.0]: https://github.com/Leaveing00001/dsh-fusion360/releases/tag/v1.0.0
