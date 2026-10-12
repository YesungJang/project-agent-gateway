# Durable Dashboard checkpoint — 2026-10-09

> **Superseded — 2026-10-10.** PRs #6–#10 were merged into `main`; the per-PR
> hold/readiness assessments below are historical. Live-service execution and
> Codex independent review remain outstanding. Kept as the audit record.

Repository: `YesungJang/project-agent-gateway` (private).
Cloud checkout: `/workspace/scratch/5ebc9ca5dd38/agent-gateway` (transient).
The durable source of truth is the GitHub branch stack and this committed record.
No Mac local working tree/configuration or Laplogos resource was modified.

## Completed implementation steps (full acceptance still pending)

| Issue | Branch | Commit | Draft PR | Tests at step |
| --- | --- | --- | --- | --- |
| #1 | `dashboard/01-event-history` | `25286280c34f9ef26be2a9d2aeb1275c3a9c9af4` | [#6](https://github.com/YesungJang/project-agent-gateway/pull/6) | 61 passed |
| #2 | `dashboard/02-pipeline-instrumentation` | `88783305facecc5f793fc7df3aed57c30e2e0504` | [#7](https://github.com/YesungJang/project-agent-gateway/pull/7) | 66 passed |
| #3 | `dashboard/03-read-only-api` | `ddf5efb3f4521c4cdc49d3959618b906c1bd2eb9` | [#8](https://github.com/YesungJang/project-agent-gateway/pull/8) | 72 passed |
| #4 | `dashboard/04-dashboard-ui` | `2c3ef4e6f21363485512aa64e9fe98dcf5f4ad2d` | [#9](https://github.com/YesungJang/project-agent-gateway/pull/9) | 75 passed |
| #5 | `dashboard/05-security-and-operations` | `4685dc59334b94a6720ce9e6562d76d082eb7010` (implementation; checkpoint refresh follows) | [#10](https://github.com/YesungJang/project-agent-gateway/pull/10) | 85 passed |

`main` remains `026f95e04f934dd680652a6c9c99b3253dbe01e0`. All original 10 commits
and their reachable objects were restored and SHA-verified, including the completed
verifier-risk diagnostic fix. Do not redo that fix or the implemented #1–#5 work.
No PR was merged, no Issue was closed, no real Gateway pipeline was run. The #2–#5
PRs target their immediate dependency branch, not `main`.
The current branch head may include this final documentation-only checkpoint refresh;
use `git log -2` / the PR head SHA for its exact revision. PR #10 contains the final
implementation and this refresh, and targets `dashboard/04-dashboard-ui`.

## Validation and remaining work

- Final Python suite: 85 tests passed, including the original 56. Default
  `http://127.0.0.1:8765` CLI startup/HTTP/shutdown smoke passed in Linux.
- Baseline/new external-operation ordering and stopping outcomes matched in 11
  synthetic scenarios (NORMAL/FAST/DEBATE, repair/plan failure, RISK/ERROR/bootstrap,
  CI pending/failure, crash). No services were contacted in these tests.
- Node syntax and 12-scenario DOM boundary checks passed (XSS-as-text, offline,
  skipped/unknown, diagnostics, repair counts). No runtime dependencies added.
- Every Issue's Codex CLI independent review was attempted read-only but produced
  no review result (authentication errors/timeouts). These are NOT passed reviews.
- Actual browser/keyboard/mobile rendering remains unverified: no local Chromium;
  the separate cloud browser cannot reach the isolated localhost. Optional browser
  harness is committed. Do not claim actual browser verification.
- macOS-specific locking/fsync/permissions/startup and actual Mac state/services are
  unverified. The original Mac working tree was unavailable.
- Lock competition is stderr-only, and task criteria wording is withheld with
  counts. Reviewer assessment of these acceptance tradeoffs remains necessary.
- GitHub Actions had zero runs when inspected. Reading combined commit status was
  denied with 403 (`Resource not accessible by integration`); CI status is unknown.
  No passing CI is claimed. A reviewer with status access must inspect it before merge.
- `.gitignore` improvements are a proposal only; actual ignore/configuration and
  original test files are unchanged. Narrow historical credential-pattern scan
  found no matches in 53 blobs through #4; this is not a proof of absence.

## Exact resume procedure

1. Read current Issues #1–#5, PRs #6 onward, branch heads, reviews and checks. Check
   whether anyone updated/merged a branch. Use actual GitHub/local state over this
   checkpoint if newer. Never force/reset an existing operator worktree.
2. In a full clone, fetch `origin`; inspect status/log/worktrees. Resume the existing
   `dashboard/05-security-and-operations` branch (or an isolated worktree of its
   latest remote head). It already includes #1–#4. Do not recreate their code or PRs.
3. Read `docs/dashboard-verification.md` and `docs/dashboard-operations.md`. Run
   `python3 -m unittest discover -s tests -v` only when validating current/new work.
   The baseline comparison requires the preserved `026f95e` Git object.
4. Run fresh local Codex CLI read-only review for each PR now that a working
   authenticated CLI is available. Keep output private unless reviewed/sanitized.
   Fix confirmed issues with focused commits and re-test; never bypass a failed gate.
5. Use a NEW external demo directory:
   `python3 demo_dashboard.py --output /tmp/gateway-dashboard-demo-NEW`, then
   `python3 dashboard.py --state /tmp/gateway-dashboard-demo-NEW --port 8765`.
   Inspect all 12 scenarios in an actual browser and at a narrow viewport, including
   keyboard/XSS/offline handling. If Playwright is already installed, the optional
   harness is `node tests/browser_dashboard.cjs`. No dependency installation is
   required for the application itself.
6. On Mac, review in a separate checkout and test the existing state read-only.
   Review lock/fsync/permissions and decide the documented criteria/lock-contention
   tradeoffs. Do not change Laplogos or active configuration to test the Dashboard.
7. Update the evidence and checkpoint. Keep PRs draft until missing verification is
   completed. Human review/merge is outstanding; retarget dependent PRs after each
   predecessor is merged and inspect the actual diff/CI. Re-run the full suite before
   proposing merge. Never let the Gateway under development merge itself.

Cloud state/demo files and CLI attempt logs outside the repository are temporary.
The 12-scenario generator and all source/tests/docs are reproducible from GitHub.

## Independent macOS follow-up — 2026-10-10

See [macOS review](dashboard-macos-review-20261010.md) for executed results and
per-PR merge assessment. Final local fixes pass all 85 tests without skips and
actual headless Google Chrome checks. At review time, the remote heads contained
macOS instrumentation fixture failures and the empty-history UI defect; fixes
`5df6d12` and `3724865` are prepared for publication to PR #10 only. Original local review commits remain preserved.
All five remote PRs remain Draft with zero checks/statuses and no approving review.
No merge, rollout, configuration or Laplogos change occurred.

## Stack integration follow-up — 2026-10-10

See [integration follow-up](dashboard-integration-20261010.md) for newer evidence.
Existing fixes are propagated to their originating #7/#9 branches and through
the stack; updated branches pass 66/72/75/85 tests on macOS. Existing Mac state
was verified read-only over HTTP and Chrome (four legacy states, no event history).
PR #10's historical mergeability failure is not reproducible; its original cause
is not established. CI and live-service execution remain unverified. No merge.
