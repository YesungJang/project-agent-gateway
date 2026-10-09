# Event history v1

The existing issue state and private command logs remain authoritative for execution.
Dashboard events are a separate, append-only observation channel outside the target
checkout: `<state>/events/run-<UUID>.jsonl`. The directory is 0700 and files are 0600.
The writer uses the same `worker.lock` descriptor as the single worker. Each invocation
has a new run UUID and a sequence starting at 1; restart does not rewrite an older run.

Every record contains version, event_id, run_id, project/repository, issue (nullable),
sequence, UTC timestamp, stage, task_id (nullable), attempt (0 outside attempts), and
outcome. Initial attempts are 1. Unknown fields are ignored; unknown versions are
reported as incomplete observation, never interpreted as success.

Only explicit metadata is recorded. No prompts, issue body/title, task title/criteria
text, source, diff, commands, stdout/stderr, configuration, environment or exception
text is stored. Task criteria are represented by a count; operators consult private
state for their actual wording. Diagnostics use fixed codes and fixed summaries.
Optional private-log references are UUIDs, never paths. CI identifiers are intentionally
publishable metadata and must not contain credentials; they are the only arbitrary
bounded text admitted by the contract.

Each line is flushed and fsynced before its sequence advances. File creation fsyncs
the event directory. A crash can leave a partial tail or a line whose fsync was not
confirmed; readers flag the partial tail and do not repair it. No append is retried.
On recording failure the integration reports a fixed observation warning, disables
further recording for that invocation, and continues the original gates and operations.
It never replays a GitHub/agent operation or substitutes an observation result for a
gate result. Missing completion is unknown, not success. A warning may be the only
available evidence when the disk is full; an entirely absent run cannot be discovered
from its absent history. Operators must inspect worker stderr/private state as well.

No automatic deletion, rotation, migration, resume or recovery is performed. Readers
have bounded line/event limits; history above those limits is marked incomplete.
Operators must monitor disk capacity and archive files manually while the worker is
stopped. Private command logs have their existing policy and are never HTTP resources.
