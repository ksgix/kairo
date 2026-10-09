# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once releases are tagged.

## [Unreleased]

No version has been tagged yet. `pyproject.toml` declares version 0.1.0. Each entry
names the commit that introduced it.

### Added

- Initial runtime foundation (`cb247bb`).
- Cognition integration (`d57efb8`) and cognition context (`18b18c0`).
- Ongoing work carried across cycles (`1e8eb5e`).
- Failure recovery (`b5c472f`).
- Multiple cognition providers (`bbb85e7`).
- Implementation package system (`2e6be6f`).
- Self-maintenance (`e2b7674`) and production installation (`22d0a8b`).
- Operator boundary, Phase 10A (`2d0d704`).
- External interaction foundation, Phase 10B (`49899fa`).
- Dashboard, Phase 11 (`1f779ba`).
- Directive descriptions, directive-bound implementations and long-lived work
  understanding (`a9564d4`).
- Reproducible Claude Code development bootstrap (`3d85416`).
- MIT license (`70e5e0c`).
- GitHub Actions workflow running the offline test suite (`eb1666d`).
- `CONTRIBUTING.md` and `SECURITY.md` (`2bcf691`).
- Issue templates (bug report, feature request, private security link) and a pull
  request template (`7b31613`).
- This changelog.

### Changed

- Repository prepared for public release (`eac74bc`).
- Project metadata completed: license, keywords, classifiers, URLs (`3952f0d`).
- README restructured into a landing page; architecture, self-maintenance, operator
  and dashboard documentation moved into `docs/` (`297853b`).
- `tests` is now a package, so the suite also runs by dotted module name (`b9ab067`).

### Removed

- Todo: `kairo.todo`, `Runtime.todo`, the `todo` situation section, the open to-do counts
  in `directives` and `open_threads`, and the Todo page of the dashboard. Nothing could
  write a todo item, and none was ever created. Existing `todo` records are no longer
  read. The operator protocol stays version 2; its `status` result no longer has the
  `open_todo` field.

### Fixed

- Restart test redaction path fragility (`9eb0ca9`).
- Dashboard browser `Origin` handling (`03e598d`).

[Unreleased]: https://github.com/ksgix/kairo/commits/main
