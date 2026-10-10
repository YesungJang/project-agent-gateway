# Independent macOS Dashboard review — 2026-10-10

Reviewed remote final head `e58e450d043aacbfb1f9ff5c79bbc7d34e72e0e8`
against `main` `026f95e04f934dd680652a6c9c99b3253dbe01e0`, in a separate
`review/dashboard-mvp-20261010` worktree. Runtime: macOS/Darwin,
Python 3.12.14 through mise, Node 24.21.0, installed Google Chrome through
temporary Playwright tooling outside the repository. No production dependency added.

## Defects fixed locally

- `5df6d12`: instrumentation tests reassigned state using unresolved macOS temporary-path aliases. The existing symlink gate correctly stopped them. Canonicalize
  the instrumentation fixture root; do not weaken Gateway path validation.
- `3724865`: after a successful poll with no runs, the UI retained the previous
  completed run summary and counters. Clear those fields. The added DOM regression
  failed before the fix and passed afterward; real Chrome also verified the fix.

At review time, both fixes were local and no push or merge had occurred.
The sanitized publication commits carry both fixes in PR #10 only; PRs #6–#9
remain unchanged. Original local review commits are preserved separately.
No force-push or gate bypass is authorized.

## Executed checks

- Final `mise exec -- python3 -m unittest discover -s tests -v`: 85 tests,
  22.779 seconds, OK, no skips. Includes original Gateway tests, macOS permissions,
  locking/fsync, HTTP privacy/boundaries, startup/shutdown and baseline-vs-new
  external-operation order/outcomes in 11 synthetic scenarios.
- Original final remote head: 85 tests, three instrumentation errors above.
- Individual remote heads: #6 61 tests OK; #7 66, #8 72, #9 75 tests each failed
  with the same three macOS instrumentation errors. These are executed results,
  not inherited checkpoint claims.
- Node syntax checks, dependency-free DOM tests including empty-history reset,
  `git diff --check` and `git fsck --full` passed.
- Real local headless Google Chrome: all 12 demo scenarios, repair counts,
  FAST/skipped, 1600x1200 desktop, 390x844 viewport without horizontal overflow,
  keyboard Tab/Enter selection, text-only XSS diagnostics, disconnected/stale
  retained view, empty-history reset, no page errors. The committed harness was
  run via a temporary copy selecting installed Chrome; additional checks stayed
  outside the repository. Native computer automation was unavailable due to
  macOS permission denial; this was actual headless browser execution.
- Screenshots inspected in an external temporary artifact directory (not committed):
  `dashboard-desktop.png`, `dashboard-mobile.png`, `dashboard-mobile-pipeline.png`.
  Full-page mobile capture showed repeated viewport imagery, so the delivered
  mobile evidence uses normal viewport captures, which rendered correctly.
- Historical high-confidence credential/private-key pattern scan: 71 unique
  reachable blobs, zero matches, zero prohibited paths encountered. No `.env`,
  `.env.*`, `~/.ssh` or `~/.aws` was read. This is not proof that no secret exists.

## Security and acceptance assessment

Reviewed event storage/projection, HTTP routes/boundaries, UI rendering and Gateway
instrumentation against Issues #1–#5. No confirmed secret/raw-log exposure or
execution-semantics regression was found. Events use structured allowlists and
fixed diagnostics, applied again when reading; HTTP has fixed static assets,
loopback-only binding, exact Host/Origin checks, no CORS or write routes, CSP and
no-store. Missing/corrupt history stays unknown. No Dashboard-triggered agent,
shell or GitHub operation was found. Existing risk/ERROR stops, repair bounds,
fingerprints, required checks, exact-head merge checks and no-auto-resume remain.

Documented acceptance tradeoffs remain: task criteria wording is withheld rather
than displayed; lock contenders produce fixed stderr rather than append events
without owning the worker lock. The heartbeat-free worker is explicitly unknown.
These require human acceptance; they are not grounds to expose raw private state.

No active Gateway, real private state/logs, Laplogos resources, credentials or
existing configuration was changed or used for browser tests. Synthetic services
do not establish that a live agent/GitHub deployment works. No second Codex CLI
review was executed; the independent review here is this Codex session.

## GitHub merge assessment

All five PRs are OPEN/Draft, MERGEABLE/CLEAN with no approving review. Every base
is an ancestor of its head: #6 → main, #7 → #6 branch, #8 → #7 branch,
#9 → #8 branch, #10 → #9 branch. GitHub Actions has no runs. For each actual PR
head, REST status has total_count=0/state=pending and check-runs total_count=0.
There is no passing CI evidence; CLEAN is only mergeability, not safety approval.

| PR | Readiness of remote head at review time |
| --- | --- |
| #6 | Implementation/tests acceptable in this review; Draft, human acceptance and CI policy remain. |
| #7 | Hold: macOS fixture tests fail; apply fixture fix and retest. |
| #8 | API review found no blocker of its own; hold for #7 correction and retest. |
| #9 | Hold: stale completed summary on empty history; apply UI fix and dependency correction. |
| #10 | Review-time hold: fixes/evidence were unpushed; subsequent publication requires head/CI verification. |

After each predecessor merges, retarget the next PR to main and inspect its actual
diff/checks. This review does not authorize merging or accepting absent CI as PASS.

## Startup

From the review checkout:

```sh
mise exec -- python3 dashboard.py --state STATE_DIRECTORY --port 8765
```

Open `http://127.0.0.1:8765`. That existing-state command is documented, not executed
here. Tested demo command uses a separate synthetic demo state directory.
The temporary demo server was stopped after browser verification.
