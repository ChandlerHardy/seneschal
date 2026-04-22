# Post-merge stewardship

When a PR merges on a repo where Seneschal is installed, the post-merge
orchestrator can:

1. **Curate the changelog** — append a Keep-a-Changelog bullet under
   `## [Unreleased]`.
2. **File followup issues** — turn `[FOLLOWUP]` markers reviewers leave
   in their reviews into tracked GitHub issues.
3. **Open a release PR** — once accumulated unreleased entries warrant
   a configured bump kind (patch / minor / major).

All three are gated on `.seneschal.yml`. **Followups and release-prep
default OFF**; changelog defaults OFF as well so installing Seneschal
doesn't change a repo's Git history without an explicit opt-in.

## Configuration

```yaml
# .seneschal.yml
post_merge:
  changelog: true                      # opt in to changelog curation
  changelog_path: CHANGELOG.md         # default; override if it lives elsewhere
  release_base_branch: main            # default
  followups: true                      # opt in to issue-filing
  followup_label: seneschal-followup   # default; renamed via this knob
  release_threshold: minor             # "" (off), "patch", "minor", "major"
  release_pr_draft: true               # default; set false to open non-draft
```

## `[FOLLOWUP]` marker syntax

Reviewers can leave a deferred-work marker in their review body:

```markdown
- [FOLLOWUP] Refactor the X module to drop the global state
  Context lines after the marker (up to 3 non-empty lines)
  are included in the issue body for context.
```

Rules:

- Case-insensitive (`[FOLLOWUP]`, `[followup]`, `[Followup]` all match).
- Up to 10 followups per review become individual issues.
- The 11th onward roll up into a single synthetic issue titled
  *"Additional follow-ups from review"*.
- Title is truncated to 100 chars; body excerpt to 500 chars.
- Re-firing the merged webhook does not duplicate issues — the
  `followups_filed` list in the review's stored frontmatter is consulted.

## Release-threshold semantics

`bump_kind` is computed from the `## [Unreleased]` block's bullets:

| Marker / prefix              | Resulting bump |
|------------------------------|----------------|
| `BREAKING CHANGE` line       | `major`        |
| `feat!:` / `fix!:` / etc.    | `major`        |
| `feat:` (any feat bullet)    | `minor`        |
| Otherwise (fix, chore, etc.) | `patch`        |

When the computed bump is `>=` the `release_threshold` setting, the
orchestrator opens (or amends) a release PR.

Threshold examples:

- `release_threshold: major` — only open a release PR when a breaking
  change accumulates.
- `release_threshold: minor` — open on any feat (or breaking).
- `release_threshold: patch` — open on every merge that produces a
  changelog entry.

## Auto-PR fallback when main is protected

The default code path direct-commits the changelog to
`release_base_branch` (`main`) using the GitHub Contents API.

If GitHub returns 403 (branch protection), the orchestrator:

1. Caches the protection state for the `owner/repo` for the rest of
   this process lifetime.
2. Switches to **auto-PR mode**: opens a branch
   `seneschal/changelog-<pr_number>`, pushes the changelog amendment
   onto it, and opens a non-draft PR labeled `seneschal:changelog`.

Subsequent merges in the same process skip the direct-commit attempt
and go straight to auto-PR mode for that repo.

## Release-PR race handling

If a release PR labeled `seneschal:release` is already open when a new
merge fires, the orchestrator amends the CHANGELOG on that PR's branch
instead of opening a second release PR.

## Labels Seneschal applies

| Label                  | Applied to              | Meaning                         |
|------------------------|-------------------------|---------------------------------|
| `seneschal-followup`   | Issue                   | Filed from a `[FOLLOWUP]` marker (configurable via `followup_label`) |
| `seneschal:changelog`  | PR (auto-PR mode only)  | Auto-PR amending the changelog  |
| `seneschal:release`    | Release PR              | Drafted release prep PR         |

## Frontmatter v2

Reviews persisted to `~/.seneschal/reviews/<owner>/<repo>/<N>.md` now
carry three optional v2 fields:

- `head_sha` — commit SHA at review time.
- `merged_at` — set by `mark_merged` when the orchestrator runs.
- `followups_filed` — list of issue numbers filed from `[FOLLOWUP]`s
  in this review's body.

v1 records (without these fields) still parse with safe defaults.
