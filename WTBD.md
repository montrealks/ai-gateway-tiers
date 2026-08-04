# Work To Be Done

## Goal

Consolidate Google Cloud projects and Cloudflare AI Gateway config into a shape where
free-tier LLM usage is structurally safe, client sites keep a working fallback, and every
project has one clear job. Driven by a $74.30 Gemini bill in July 2026 caused by a BYOK key
from a billing-enabled project.

End state — 6 Google projects, only 2 billed:

| Project | Billed | Job |
|---|---|---|
| pressiveweb | no | agency tooling (GSC/GA4/PageSpeed) + OAuth logins for snave, 1981, CNXLocal, Pressive |
| route1views | **yes** | production Maps/Places/Vision — genuinely high usage |
| places-scout *(new)* | **yes** | shared Places tooling for Kris + Lily |
| kboodle | no *(after unbilling)* | client-owned (Bob co-owner): its own OAuth + its own free Gemini quota |
| gemini-free-tier | no | Kris's free Gemini — single-purpose so billing can never attach |
| earnest-vine | no | Photos Picker for route1views |

cnxlocal retires once places-scout traffic has moved off it.

Per client gateway the chain becomes: **Gemini free (with retries) → Azure (free to ~2026-09-21)
→ Anthropic (CF unified, paid)**. Gateway-side dynamic routes, so no chain logic in PHP.
The Python tier client stays for local/Python work only.

### Constraints

- **route1views is the oldest client and does not tolerate disruption.** Any task touching its
  production path is marked FLAG FIRST: research and propose, do not execute unattended.
- Pause and report on anything that confounds the plan rather than working around it.
- Serial execution, not parallel lanes. These tasks mutate shared live infrastructure (the same
  Google projects and CF gateways), so they are not file-disjoint; and this session's standing
  instruction is not to dispatch subagents. Skill parallelism defers to that.

## Execution Protocol (read every iteration)
1. Every iteration tackles exactly ONE task.
2. Read the Progress notes left by the previous iteration before starting.
3. Full research for the task at hand — no hallucination, no laziness. Before starting,
   identify the back-pressure (a test, check, command, or observable signal) you will use to
   prove the task afterward.
4. Do the work. When changing config, apply it everywhere it appears — search for EVERY
   occurrence; never patch one site.
5. Run the smoketest after the task, plus the back-pressure from step 3.
6. Commit (repo changes only). Never mention Claude / Claude Code / AI in the commit message.
7. Mark the task complete here (check its box).
8. Leave a dated note under Progress.
9. One task at a time. Do not stop until every task is checked off, except at a FLAG FIRST task.

**Smoketest:** `python3 scripts/selftest.py` in this repo — proves all five tiers, the client
profile, and the free-tier billing guard are green.

**Baseline harness:** `python3 scripts/audit_estate.py` — captures the full Google + Cloudflare
picture to `.tmp/estate-<label>.json`. Run before and after; diff at the end.

## Tasks

- [x] A. Delete kboodle's dead Gemini API key *(done before this plan existed; the key was
      already non-functional since its API was disabled, so it cannot affect baseline capability)*
- [x] B. Build `scripts/audit_estate.py` and capture the BASELINE snapshot
- [x] C. Lock down cnxlocal "API key 3" — unrestricted across ~32 Maps APIs on a billed project
- [x] D. Delete kboodle's Maps key *(Kris confirmed the ACF map wiring is leftover, unused)*
- [x] E. Unlink billing from the kboodle project
- [x] F. Re-enable generativelanguage on kboodle and mint its own Gemini key
- [x] G. Repoint `kboodle_google-ai-studio_default` to kboodle's own key (stop spending Kris's quota)
- [x] H. Create the `places-scout` project — billing, Places API, restricted key
- [ ] I. Migrate the places-scout Worker to the new key and verify traffic moves projects
- [ ] J. Store Azure secrets on the kboodle and route1views gateways
- [ ] K. Create dynamic routes on kboodle, helloplaydate and profilo gateways
- [ ] L. Add google-ai-studio secrets for the helloplaydate and profilo gateways
- [ ] M. **FLAG FIRST** — add Azure step + retries to route1views' existing `low` route
- [ ] N. **FLAG FIRST** — restrict route1views' unrestricted "Maps Server Key"
- [ ] O. Retire the cnxlocal project *(conditional: only after I has shown a clean week)*
- [ ] P. Re-run the harness, diff against baseline, update README/tiers.json to match reality

## Progress
<!-- newest note first; one entry per completed task -->

### 2026-08-04 — Task H
Created Google project `places-scout-kris` (display name "places-scout"). The id `places-scout`
was unavailable, so the id and display name differ — per the naming lesson from the July
incident, always identify this one by ID.

Linked billing (Places API requires it), enabled ONLY `places.googleapis.com`, and minted key
`eb4cf64d` "Places Scout" scoped to that single API.

Rationale: places-scout is shared tooling for Kris AND Lily across multiple areas, so billing it
to the `cnxlocal` client project mis-attributed the cost. This gives it its own home and makes
cnxlocal retirable.

Back-pressure: new key returns 200 from `places:searchText` ("Fern Forest Cafe").

Next (task I): swap the Worker secret. The worker reads `GOOGLE_PLACES_API_KEY`; it lives at
~/Projects/cnxlocal/workers/places-scout/. Wrangler needs CLOUDFLARE_API_TOKEN in env — it is
NOT exported by default (keychain-gated behind `cf-load`), so that command may need Kris to run
it, or the key read from the keychain inline.

### 2026-08-04 — Tasks D, E, F, G (+ machine reboot)
Kris confirmed the ACF Google Map wiring is leftover and unused, unblocking D. Both PHP call
sites (`ThemeProvider::configureAcfGoogleMaps`, `AssetProvider::setGoogleMapsApiKey`) have
`?: ''` fallbacks, so they degrade to an empty key rather than erroring.

- **D** — deleted kboodle API key `2a0350ac`. Project now has no Maps key.
- **E** — unlinked billing from the kboodle project. `billingEnabled: false`.
- **F** — re-enabled `generativelanguage` (safe now the project cannot be charged) and minted
  key `33857af1` "kboodle Gemini (free tier)", scoped to that one API. Verified 200 directly.
- **G** — repointed secret `17622324` so the kboodle gateway uses kboodle's OWN key. Bob's site
  now draws on Bob's free quota instead of Kris's.

**kboodle is now the target end state**: unbilled, client co-owned, its own OAuth, its own free
Gemini quota, and structurally unable to generate a bill.

Machine rebooted mid-task. State verified intact afterwards: env vars present (GEMINI_API_KEY
correctly served from .zshrc, so the earlier launchctl-only fragility is resolved), gcloud still
authenticated, kboodle state as above.

Back-pressure: kboodle gateway returns 200 'pong'; smoketest green.

Note: `wp-config.php` still defines `KBOODLE_GOOGLE_MAPS_API_KEY` pointing at the deleted key.
Harmless (both consumers fall back to ''), but worth removing next time that file is touched.

### 2026-08-04 — Task D BLOCKED (plan confounded)
My earlier "dead code path" finding was WRONG — it came from a grep scoped only to `src` for the
accessor name. Widening it found two real consumers:

- `ThemeProvider::configureAcfGoogleMaps()` — registers an `acf/fields/google_map/api` filter,
  i.e. the key powers ACF's **Google Map field** in wp-admin.
- `AssetProvider::setGoogleMapsApiKey()` — localises the key into front-end scripts.

That explains the tiny usage (4 calls in 70 days): an admin map picker only calls Google when an
editor actually opens the field. Low usage, but live functionality.

Consequences:
- Task D (delete the key) would break the admin map picker.
- Task E (unbill kboodle) would too — Maps Platform requires billing.
- Which breaks tasks F/G (kboodle's own free Gemini quota), since free tier needs an unbilled
  project.

Could NOT determine whether an ACF `google_map` field actually exists: none is defined in theme
code, but ACF field groups can be created in the DB via the UI, and ddev is not running.

Options put to Kris — awaiting his call before proceeding past D.

### 2026-08-04 — Task C
Narrowed cnxlocal "API key 3" (`2b6802f8`) from **32 API targets to 10** — the set with observed
traffic in the last 30 days (places, places-backend, maps-backend, geocoding, directions,
distance-matrix, elevation, timezone, static-maps, street-view).

Research: nothing in the cnxlocal repo references this key. The only Google key usage there is
the places-scout worker, which reads `GOOGLE_PLACES_API_KEY` — that's the separate "Places Scout"
key, already scoped to places only. But API key 3 did make ~8 calls in 30 days across
Directions/Elevation/Timezone/StreetView, so an unknown caller exists. Narrowing rather than
deleting keeps that caller working while cutting blast radius; deletion is the follow-up once
cnxlocal retires in task O (console offers "Restore deleted credentials", so it's recoverable).

Back-pressure: both cnxlocal keys still return 200 from places:searchText after the change, and
the smoketest is green.

NOT fixed: the key still has no referrer/IP restriction, so possession is still sufficient to use
it. Can't lock that down without knowing whether the caller is browser or server-side. Revisit in
task O when the project retires.

### 2026-08-04 — Task B
Built `scripts/audit_estate.py` (snapshot + `--diff`) and captured `.tmp/estate-baseline.json`.
Records per-project billing/services/keys/usage and per-gateway secrets/routes, then LIVE PROBES
three paths. Back-pressure: all three probes returned ok.

Baseline facts worth carrying forward:
- No project can currently bill for LLM (`can_bill_for_llm` false everywhere) — the earlier
  disable of generativelanguage on kboodle closed the last exposure.
- Only route1views has a dynamic route: `low` = google-ai-studio -> anthropic. No Azure step.
- profilo gateway has NO secrets at all; helloplaydate has azure+fal but no google.
- kboodle + route1views gateways have google secrets but NO azure — needed before task K/M.

Known gap in the harness: the `unrestricted` flag only fires when a key has no API targets AND
no referrer AND no IP restriction. cnxlocal "API key 3" has 32 API targets but no referrer/IP
lock, so it reads as restricted. Task C addresses the key itself; tighten the heuristic in P.

### 2026-08-04 — Task A (pre-plan)
Deleted kboodle API key `f9597cb2` ("Generative Language API Key"). Safe because
`generativelanguage.googleapis.com` had already been disabled on that project earlier in the
session, so the key was inert. Verified: `gcloud services api-keys list --project=kboodle` now
returns only "API key 1".

Note for next iteration: this happened *before* the baseline harness existed, so the baseline
will not include it. Acceptable — an inert key has no capability to measure.
