# Cloudflare Pages migration plan (Phase 0)

> **Status: deferred (Joe, 2026-05-22), not yet reprioritized.** Joe parked the migration on 2026-05-22 to ship the digest service first (issue #9). That digest has since gone live (running as `digest-cron.timer`, 146 tests as of 2026-06-02 per `CLAUDE.md`), so the original "digest first" reason is satisfied — but the migration is still parked pending Joe picking it back up, and it is gated on the cross-account blocker below. This doc is the executable plan for that restart. It exists because the digest plan and design spec both point at it as a blocking prerequisite, yet the file was never written.
>
> **Filename note.** The `2026-05-08` prefix is the project-epoch convention shared by the design spec and the digest plan, and it matches the dangling reference at `2026-05-08-eventbrite-attendee-digest.md:13`. The plan body was authored 2026-06-04 (wake session) and the current-state facts were verified that day.
>
> **Companion docs:** design spec `docs/superpowers/specs/2026-05-08-eventbrite-attendee-digest-design.md` (Phase 0 section), digest plan `docs/superpowers/plans/2026-05-08-eventbrite-attendee-digest.md` (Phase 5 blocks on this plan's Phase 0c), tracking issue jamditis/eventbrite-automation#10 (carries the 2026-05-22 reconnaissance).

## Goal

Move `pages.centerforcooperativemedia.org` from GitHub Pages to Cloudflare Pages so that Cloudflare Access can gate the future admin UI and API routes (`/events/*/admin`, `/api/*`).

## Why this is required

Cloudflare Access can only enforce on traffic that traverses Cloudflare's proxy. GitHub Pages requires the `pages.*` record to be DNS-only (grey cloud) to serve, which routes around Access. The two are mutually exclusive on the same origin, so the site has to move to a Cloudflare origin (Pages) before any Access policy or Pages Function can run. This is the sole reason for the migration — there is no content or performance driver.

## Current state (verified 2026-06-04)

| Fact | Value | How verified |
|---|---|---|
| Prod origin | GitHub Pages (`server: GitHub.com`) | `curl -I https://pages.centerforcooperativemedia.org/` |
| Prod `last-modified` | 2026-05-22 15:45 GMT | same response header |
| `pages.*` DNS | CNAME → `jamditis.github.io` (DNS-only) | `dig CNAME pages.centerforcooperativemedia.org` |
| Zone nameservers | `lia.ns.cloudflare.com`, `lynn.ns.cloudflare.com` | `dig NS centerforcooperativemedia.org` |
| CF Pages project | `ccm-pages`, serving `ccm-pages.pages.dev` (HTTP 200) | `curl -I https://ccm-pages.pages.dev/` |
| CF preview freshness | stale — built 2026-03-03 per the recon; serves an old snapshot | issue #10 comment, 2026-05-22 |
| Source repo | `github.com/jamditis/ccm`, CNAME `pages.centerforcooperativemedia.org` | repo `CNAME` file |
| Submodules | `cjs2026`, `njcic`, `cjs-beat-street`, `tools` | `.gitmodules` |
| GH Pages deploy | `.github/workflows/pages.yml` — push to `main` + `workflow_dispatch`, `submodules: false` | workflow file |
| Daily events cron | `.github/workflows/update-events.yml` — `cron: 0 6 * * *`, auto-commits `index.html` to `main` | workflow file |
| Last events auto-commit | 2026-05-08 (`b3ea1a2`) | `git log origin/main` |
| Manual CF deploy | `deploy.sh` — `wrangler pages deploy`, inits submodules, excludes files >25 MB, account `3d4b1d36…` | `deploy.sh` |

So Phase 0a is **partly done** (a Pages project exists) but stale, and there is no deploy automation for Cloudflare Pages.

## The blocker: cross-account split (Joe decision required)

This is the gate that stops everything else. Per the 2026-05-22 reconnaissance (issue #10), the zone `centerforcooperativemedia.org` is on Cloudflare nameservers (confirmed above) but the zone sits in a **different Cloudflare account** than the one that owns `ccm-pages` (`deploy.sh` targets account `3d4b1d36…`, Joe's personal account). `dig` can confirm the nameservers but cannot reveal which account owns the zone — only the Cloudflare dashboard can.

Cloudflare Access policies and a Pages custom domain must live in the account that owns the zone. If the Pages project is in account A and the zone is in account C, Access on `pages.centerforcooperativemedia.org` cannot be wired up. So the Pages project, its custom domain, and the Access policies should all co-locate with the CCM zone.

**Open question for Joe:** which Cloudflare account owns `centerforcooperativemedia.org`, and does Joe have admin access to it? The answer picks the path:

- **Scenario A — zone is in Joe's personal account (`3d4b1d36…`).** No move needed. `ccm-pages` already lives there. Proceed straight to 0a (fresh redeploy) → 0b → 0c.
- **Scenario B — zone is in a CCM/Montclair account Joe can admin.** Recreate the Pages project in that account (or move it), repoint `deploy.sh`'s `CLOUDFLARE_ACCOUNT_ID`, then 0a → 0b → 0c there.
- **Scenario C — zone is in an account Joe cannot admin.** Migration is blocked until access is obtained or the zone is transferred. Do not cut over; keep GitHub Pages.

Resolve this before doing any 0b/0c work — a cutover into the wrong account strands the Access integration that is the entire point of the migration.

## Findings from the 2026-06-04 verification that change the plan

These were not in the original spec or the 2026-05-22 recon and they reshape the acceptance gates.

1. **The spec's "live paths" list is not a clean parity target.** 8 of the 18 paths the spec lists as "existing live paths to verify" return 404 on prod right now (measured 2026-06-04). They break into three causes, so "all listed paths return 200" is unachievable by construction and must not be the gate:
   - **Submodule roots are dark on GitHub Pages.** `/njcic/`, `/cjs2026/`, and `/cjs-beat-street/` 404 on prod because `pages.yml` checks out with `submodules: false`, yet each carries an `index.html` in its submodule (verified). `deploy.sh` inits submodules, so Cloudflare *restores* these — `ccm-pages.pages.dev/njcic/` already returns 200. The migration is a net gain here, not just parity. `/tools/` is a skipped submodule too, but it has no `index.html`, so it stays 404 either way — deploying the submodule does not give it a landing page.
   - **Directories with no index document.** `/fellowships/`, `/programs/`, `/internal-tools/`, and `/reports/` have no `index.html` (and in three cases no HTML at all), so they 404 on both origins. They were never live landing pages; drop them from the gate.
   - **Directories whose live page is a named file.** `/demoday/` and `/weekender/` 404 as directory roots but their named files serve (`/demoday/demoday-insights-2025.html` → 200, `/weekender/weekender-2025-template.html` → 200). The real live path is the file, not the directory.

2. **Cloudflare Pages 308-redirects `.html` URLs to extensionless (clean URLs).** Measured on the preview: `/demoday/demoday-insights-2025.html` returns 200 on GitHub Pages but **308 → `/demoday/demoday-insights-2025`** on Cloudflare. 308 is followed by browsers, so links keep working, but canonical URLs change and any `og:url`/`<link rel=canonical>` pointing at a `.html` path becomes a redirect. Decide before cutover: accept the clean-URL redirects (and audit canonical tags), or suppress them with a `_redirects`/Pages setting so `.html` paths stay canonical. This is the single most likely source of a "looks broken" soak report.

3. **The Cloudflare preview is a stale 2026-03-03 build, so its path results are indicative, not authoritative.** `ccm-pages.pages.dev/demoday/` returns 200 even though current source has no `demoday/index.html`, which means the snapshot predates a content change (or Cloudflare is serving an old artifact). Phase 0a must redeploy from current `main` and re-measure before any path comparison is trusted.

4. **Two deploy paths still target GitHub Pages.** `pages.yml` fires on every push to `main`, and `update-events.yml` pushes a daily `index.html` auto-commit. After cutover, both of those pushes must deploy to Cloudflare Pages instead, or prod goes stale the moment the events cron next commits. `deploy.sh` is manual only and is not wired into CI.

5. **Events-cron health is unconfirmed, and commit history cannot confirm it.** The last `update-events.yml` auto-commit was 2026-05-08 (27 days before this writing). A no-diff day produces no commit, so a healthy daily cron that simply found no event changes looks **identical** to a cron that is silently failing to fetch — both leave green-looking history with no commits. So "recent commits" is the wrong signal. Confirm the scheduled runs themselves are firing and succeeding: `gh run list --workflow=update-events.yml -R jamditis/ccm --limit 10` should show recent `schedule`-triggered runs with `success` conclusions. A cutover while the cron is silently dead bakes a staleness bug into the new origin that nobody notices until content goes stale weeks later.

## Current prod baseline (regression fixture, measured 2026-06-04)

Capture this as the cutover regression target: the gate is **no path that is 200 today may regress to non-200 after cutover**, plus the submodule paths are expected to *improve* from 404 to 200.

| Path | Prod (GH Pages) | Note |
|---|---|---|
| `/` | 200 | |
| `/njnewswire/` | 200 | |
| `/ecm/` | 200 | |
| `/ecm/awards.html` | 200 | `.html` → expect 308 on CF (clean URL) |
| `/ecm/speakers.html` | 200 | `.html` → expect 308 on CF |
| `/ecm/schedule.html` | 200 | `.html` → expect 308 on CF |
| `/ecm/sponsors.html` | 200 | `.html` → expect 308 on CF |
| `/jobs/` | 200 | |
| `/foodaccess/` | 200 | |
| `/edit/` | 200 | |
| `/demoday/demoday-insights-2025.html` | 200 | directory root 404s; named file is the live path |
| `/weekender/weekender-2025-template.html` | 200 | directory root 404s; named file is the live path |
| `/njcic/` | 404 | submodule — expect **200** on CF (improvement) |
| `/tools/` | 404 | submodule, no index — stays 404 unless an index is added |
| `/cjs2026/` | 404 | submodule — expect **200** on CF (improvement) |
| `/cjs-beat-street/` | 404 | submodule — expect **200** on CF (improvement) |
| `/fellowships/`, `/programs/`, `/internal-tools/`, `/reports/` | 404 | no index document — not live paths; drop from gate |

Re-run this sweep against the fresh preview in 0a and against prod in 0c.

## Phases

### Phase 0a — fresh Pages build and preview verification

- [ ] Resolve the account-split decision (Scenario A/B/C) before touching the project. Under B, recreate `ccm-pages` in the zone's account and repoint `deploy.sh`'s `CLOUDFLARE_ACCOUNT_ID`.
- [ ] Clean and scope the `ccm` working tree first — it carries submodule-pointer drift plus untracked dirs (e.g. `social-scraper/` round-2 artifacts, `docs/`, `prompts/`). The migration touches only deploy config; do not sweep unrelated content into it.
- [ ] Redeploy `ccm-pages` from current `main` (`bash deploy.sh`, or the CI build settings with `git submodule update --init --recursive`).
- [ ] Confirm the events cron is firing and succeeding — `gh run list --workflow=update-events.yml -R jamditis/ccm --limit 10` should show recent `schedule`-triggered runs with `success` conclusions, not just an absence of commits (see finding 5; commit history cannot prove the cron is alive).
- [ ] Re-run the baseline sweep against `ccm-pages.pages.dev`. **Acceptance:** every path that is 200 on prod today is 200 on the preview; submodule roots (`/njcic/`, `/cjs2026/`, `/cjs-beat-street/`) are 200; decide and record the `.html` clean-URL behavior (accept the 308s or suppress them).

### Phase 0b — DNS cutover

- [ ] Add `pages.centerforcooperativemedia.org` as a custom domain on the `ccm-pages` project (in the zone's account).
- [ ] Repoint DNS: `pages.*` CNAME from `jamditis.github.io` to the Pages target, proxied (orange cloud) so Access can later enforce. **Acceptance:** `curl -sI https://pages.centerforcooperativemedia.org/ | grep "server: cloudflare"` returns the header (this is exactly the check the digest plan's Phase 5 Task 17 runs), TLS is valid, and the baseline sweep shows no regression on the walkthrough.

### Phase 0c — soak

- [ ] Add a Cloudflare Pages deploy on push to `main` so the daily events auto-commit ships to Cloudflare. **Keep `pages.yml` building GitHub Pages in parallel through the soak** — it is the warm rollback target, so do not stop or delete it at soak start. A stale GitHub Pages artifact would turn rollback from a one-line DNS flip into a rebuild.
- [ ] **Acceptance:** clean Cloudflare Pages deploy logs, the full baseline sweep returns the expected codes against prod, at least one CCM staffer other than Joe confirms no regression in their workflow, and no inbound bug reports.
- [ ] **Only after acceptance passes:** retire `pages.yml` (the GitHub Pages deploy). Until then it stays live as the rollback target.

## Rollback

Flip the `pages.*` CNAME back to `jamditis.github.io` (DNS-only). The GitHub Pages deployment stays intact until Phase 0c acceptance passes, so rollback is a single DNS change with no rebuild.

## Open questions for Joe

1. **Account ownership (blocking).** Which Cloudflare account owns `centerforcooperativemedia.org`, and can Joe admin it? Picks Scenario A/B/C.
2. **Clean URLs.** Accept Cloudflare's `.html` → extensionless 308 redirects (and audit `og:url`/canonical tags across the site), or suppress them to keep `.html` paths canonical?
3. **Submodule paths.** `/njcic/`, `/cjs2026/`, and `/cjs-beat-street/` are dark on GitHub Pages today and would come back live after the migration — confirm those sections should be public before cutover restores them.
