# Dashboard startup and operations

## Inspect in a separate checkout

Do not replace or run the active Gateway with unreviewed development code. Do not
point the Gateway at its own development repository to merge these changes. The
implementation is a stack of focused draft PRs; no PR has been merged. The final
branch includes all earlier changes without changing `main`.

On the Mac, first inspect the original checkout and existing worktrees:

```sh
cd ~/dev/agent-gateway
git status --short
git worktree list
git fetch origin
```

For the first review, create a separate checkout (choose a different unused name if
the worktree/branch already exists; never reset or delete existing work):

```sh
git worktree add -b review/dashboard-mvp ../agent-gateway-dashboard origin/dashboard/05-security-and-operations
cd ../agent-gateway-dashboard
python3 -m unittest discover -s tests -v
python3 dashboard.py --state "$HOME/.config/agent-gateway/gateway-state" --port 8765
```

Use a Python 3.12+ environment; `mise exec -- python3 ...` can replace `python3` if
that is how your existing environment selects Python. No install step or production
dependency is added. Open `http://127.0.0.1:8765` explicitly (Host=`localhost` is
intentionally rejected). Stop the observer with Ctrl-C. Do not change bind address,
proxy it, open a firewall rule or publish it externally. The cloud session tested
CLI startup plus real loopback HTTP in Linux, not on macOS.

Starting the observer on existing private state makes no worker/configuration writes.
An old deployed Gateway does not produce new events. Its existing state appears as
legacy state with unknown stage/time. Adopting the instrumented Gateway is a separate
operator-controlled rollout after review; no active Mac installation/configuration,
Laplogos checkout, issue or PR was changed by this work.

## Isolated synthetic inspection

If real state is unavailable, choose a NEW external directory:

```sh
python3 demo_dashboard.py --output /tmp/gateway-dashboard-demo-NEW
python3 dashboard.py --state /tmp/gateway-dashboard-demo-NEW --port 8765
```

Existing directories and paths inside this checkout are refused by the generator.
It makes synthetic examples only and performs no agent, GitHub or Gateway execution.
Never generate demo history into active state. The 12 examples include completed,
repair/repair limit, ERROR/RISK, bootstrap failure, CI pending/failure, crash, cut
tail, FAST and DEBATE. Their PR links point to a fictional `example/gateway` repo.
Some examples are intentionally stale or incomplete.

## State, durability and freshness

The trusted worker state remains external. Events are only in `events/` (0700),
each `run-UUID.jsonl` file is 0600. The existing `worker.lock` remains the single
worker lock. Every new invocation has a new run identity; existing issue state
prevents automatic duplicate processing. Event file creation syncs the event and
parent state directories; each completed append flushes/fsyncs the file.

No automatic deletion/rotation/resume is added. Monitor capacity before long-term
use. Readers show unknown quality on their scan/file/event/line limits; archive
older event files manually with the worker stopped rather than silently losing
records. Existing raw command logs remain private and have no HTTP route.

Queue data is the last recorded fetch (up to 100 observed issues), not the complete
live GitHub queue. The UI polls local files every five seconds. A connected API is
not evidence of worker liveness. No heartbeat exists during commands: start-only
runs are unknown and become stale after 60 seconds. An exited single-shot worker is
normal. Task/run history and current worker health are separate.

## Investigate a stop or missing history

1. Check the last successful API observation and event/queue ages; API failure keeps
   a visibly stale last view. Restarting the observer is read-only and does not
   restart or approve the worker.
2. Inspect fixed diagnostics, run/task gate results and attempt/repair counts. ERROR
   and RISK require operator review and are not ordinary FAIL repair loops.
3. Inspect private issue state and private logs locally for actual criteria and full
   failure evidence. Do not paste secrets or raw prompts into PRs or the Dashboard.
4. Inspect stderr for `OBSERVATION_FAILED` or `WORKER_LOCK_CONTENDED`. A failed
   observer may leave no discoverable event run; lock contenders emit stderr only.
5. Preserve worktree/state/history. Check actual PR/head/base/CI in GitHub manually
   when needed. Missing end, cut tail, unknown version or crash never authorizes
   rerun, repair, approval or merge. The original no-auto-resume rule remains.

## Review and rollout

Review the stack from #6 → #7 → #8 → #9 → #10. Subsequent PRs
target their dependency branch. After reviewing/merging a predecessor, retarget the
next PR to `main` and inspect its diff/CI; resolve conflicts without force or bypass.
Re-run the full Gateway suite before any merge proposal. Independent Codex CLI and
real-browser checks remain required. Nothing here authorizes Gateway self-merge.

Future UI approval/resume, scheduler, multiple-project operations, retention period,
and external exposure require separate design and authorization.
