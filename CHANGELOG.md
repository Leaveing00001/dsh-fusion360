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
