"""Post-merge stewardship: changelog curation, release prep, followup tracking.

When a PR merges, the orchestrator (orchestrator.py) sequences three optional
behaviors gated on per-repo `.seneschal.yml` config:

  - Changelog: append the merged PR to `## [Unreleased]` in CHANGELOG.md.
  - Followups: file GitHub issues for `[FOLLOWUP]` markers in the stored review.
  - Release: when accumulated unreleased entries cross a threshold, open a draft
    release PR.

Pure modules (no I/O):
  - changelog.py — Keep-a-Changelog formatting + insertion
  - release.py   — semver bump + release-notes rendering
  - followups.py — `[FOLLOWUP]` marker parsing

Glue:
  - orchestrator.py — disk + GitHub I/O sequencing
"""
