# Single-worker observation

`Gateway.once()` creates a fresh event run only after acquiring its existing
nonblocking worker lock. Queue retrieval is a timestamped snapshot limited to 100
observed issues, not a claim about all GitHub issues. Opening the Dashboard never
refreshes this snapshot through GitHub. Claim, worktree, bootstrap, context, DEBATE,
plan/gates, tasks/validation/repair/commit, full validation, PR preparation/publish,
CI/Merge Gate, confirmed merge/close and termination have explicit observations.
Task identity is its stable plan index (`task-1`, etc.); attempts include the initial
attempt. Plan and task repairs carry separate phases and counts.

FAST records skipped independent gates and merge stages, never fictitious gate
success. Dry-run PR readiness is local preparation; no published PR is inferred.
Published PR links are reconstructed from the verified repository and numeric PR
identity. Required CI uses the exact existing percent-encoded identities and the
existing deterministic `required_check_gate` interpretation, including duplicates.

No heartbeat is added to long-running commands. A start observation proves only that
the worker started, not that it remains alive. Health during a run is unknown (stale
after a freshness threshold); PID is metadata and is never treated as proof. An idle
single-shot invocation terminates normally and is not a worker outage. A missing end
observation is unknown/crash-possible, not an automatic restart request.

Lock competition is recorded as the fixed `WORKER_LOCK_CONTENDED` stderr diagnostic.
The losing process writes no event history because it does not own `worker.lock`.
This deliberate exception to event-history coverage preserves the single-writer
contract and prevents a contender from changing the running worker's health.
SIGKILL/power loss cannot produce an exit event; the reader must show unknown/stale.

Observation write/initialization/close failures emit fixed stderr warnings, suppress
further event writes and leave all original operation order, labels, gate outcomes,
repair limits and stopping/resume rules intact. Raw private reasons remain in private
state only. The Dashboard supplies fixed diagnostic summaries and criteria counts;
actual issue/task wording is intentionally withheld to avoid prompt disclosure.
