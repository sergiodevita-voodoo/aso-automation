# Onboarding a New Game to the PGT ASO Automation

This document is the manual procedure. For 99% of cases use the
[`pgt-setup-aso-automation`](https://github.com/VoodooStudios/claude-skills-marketplace/tree/main/packs/game-dev/voodoo-game-dev/pgt-setup-aso-automation)
skill, which automates everything below.

---

## Prerequisites (one-time, per game)

These cannot be automated — they require PubOps action:

1. **CircleCI deployer config branch** on `VoodooTeam/vs-ci-deployer`:
   `feature/<GameName>_<UnityVersion>` must exist (e.g. `feature/BallsGoHigh_6000.0.72f1`).
   Set up via the `pgt-setup-circleci` skill if not already.

2. **CircleCI Context `VS_CI_<GAME_NAME_UPPER>`** must exist with the game's
   signing + Firebase env vars. Created by `pgt-setup-circleci` or PubOps.

3. **Play Console Service Account grant** — PubOps must add
   `icon-updater@pgt-icon-update.iam.gserviceaccount.com` with **Release manager**
   role on the game's Play Console app (and the `Manage app content` checkbox).
   The 6 checkboxes are:
   - View app information and download bulk reports
   - Create and edit draft apps
   - Edit store listing, pricing & distribution
   - Manage testing tracks and edit tester lists
   - Release to testing tracks
   - Release to production, exclude devices, and use Reach and devices

4. **App Store Connect access** — the shared ASC API key
   (id `5LCI08GKIV3O`) already has access to every game your Voodoo team
   owns. No per-game action needed.

5. **GitHub Actions enabled** at the repo level — defaults on for new repos
   in `VoodooStudios` org. Verify via
   `gh api /repos/VoodooStudios/<RepoName>/actions/workflows`.

---

## Org-level prerequisites (one-time, ever)

These are set up ONCE at the `VoodooStudios` org level and reused by every
game's automation:

1. **6 org-level GitHub Secrets**, scoped to PGT game repos:
   - `SCENARIO_API_KEY`
   - `CIRCLECI_TOKEN`
   - `ASC_KEY_ID`
   - `ASC_PRIVATE_KEY_P8`
   - `GOOGLE_PLAY_SA_JSON`
   - `ANTHROPIC_API_KEY`

   Set via `Org Settings → Secrets and variables → Actions → New organization secret`.
   Repository access: `Selected repositories` → list the PGT game repos.

2. **Actions in this organization** — `Org Settings → Actions → General →
   Allow VoodooStudios actions and reusable workflows`. Required so each
   game's workflow can call `VoodooStudios/aso-automation@v1`.

---

## Per-game procedure (4 files + 1 skill or manual)

For each new game's repo:

### 1. Add `aso-automation.config.yml` at the repo root

Use [`templates/aso-automation.config.yml.template`](./templates/aso-automation.config.yml.template),
fill in the `{{placeholders}}`. See examples:
- [DribbleHoops](https://github.com/VoodooStudios/DribbleHoops/blob/develop/aso-automation.config.yml)
- BallsGoHigh (staged in this repo at `staged-per-game/BallsGoHigh/`)

Key per-game values:
| Key | How to find |
|---|---|
| `game.name`, `game.code`, `game.slug` | Game's `CLAUDE.md` |
| `game.repo_url` | `git remote get-url origin` |
| `game.default_branch` | `git symbolic-ref refs/remotes/origin/HEAD` (usually `main`) |
| `stores.ios.bundle_id` | Game's `ProjectSettings/ProjectSettings.asset` (`iPhone:` line under `applicationIdentifier`) |
| `stores.ios.apple_app_id` | ASC API `GET /v1/apps?filter[bundleId]=<bundle>` |
| `stores.android.package_name` | Same `ProjectSettings.asset`, Android line |
| `icon.paths_to_overwrite` | One-line list if Unity auto-resizes from a master, or per-size variants if explicit |
| `versioning.project_settings_file` | Path to `ProjectSettings.asset` (root or nested) |
| `build.circleci_config_branch` | `feature/<GameName>_<UnityVersion>` |
| `build.android_keystore_name` | From the CC context's env vars |
| `notifications.slack_channel` | `#pgt-<game-slug>` |

### 2. Add `.github/workflows/aso-biweekly.yml`

Copy [`templates/aso-biweekly.yml.template`](./templates/aso-biweekly.yml.template),
substitute `{{game_name}}` in the description.

### 3. Set up `develop` branch if absent

PGT convention requires `develop` to be the integration branch. If the
repo only has `main`/`master`, create `develop` off it and configure
branch protection so direct pushes work for the automation.

### 4. Sanity smoke test

```bash
gh workflow run aso-biweekly.yml --repo VoodooStudios/<RepoName> --ref develop -f dry_run=true
```

A dry-run skips the push/CI/store steps but runs Scenario + Claude.
Costs ~$0.50 in Scenario/Anthropic credits. Use this to validate the
config before the first real run.

### 5. First real run

```bash
gh workflow run aso-biweekly.yml --repo VoodooStudios/<RepoName> --ref develop -f dry_run=false
```

Or wait for the biweekly cron (`0 9 1,15 * *` UTC).

---

## Verification checklist after onboarding

- [ ] `aso-automation.config.yml` exists on `develop`
- [ ] `.github/workflows/aso-biweekly.yml` exists on `develop`
- [ ] Workflow appears in `gh workflow list --repo VoodooStudios/<game>`
- [ ] 6 org secrets visible as `Inherited from organization` in the game repo's Secrets tab
- [ ] PubOps confirms Play SA granted
- [ ] CC context for the game exists + has the right env vars
- [ ] Dry run produces a new icon + What's New (visible in workflow logs)
- [ ] First real run lands new version on both stores

---

## Bumping the action version

```yaml
- uses: VoodooStudios/aso-automation@v1
```

If we tag `v2` with breaking changes, each game's workflow needs to bump
the version explicitly. For non-breaking hotfixes we move the `v1` tag
forward — every game picks up the change on its next run.