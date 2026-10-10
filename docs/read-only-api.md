# Local read-only observer API

Start independently of the single-shot worker:

```sh
python3 dashboard.py --state /absolute/path/gateway-state --port 8765
```

Only `127.0.0.1` is bound; there is no host/bind option. Use
`http://127.0.0.1:8765` rather than `localhost`. Stop with Ctrl-C. No configuration,
credentials, shell command, approval control or GitHub client is loaded. Starting,
reading or stopping the observer does not start/stop the Gateway or alter its state.

GET endpoints:

| Route | Data |
| --- | --- |
| `/api/snapshot?cursor=<sha256>` | Queue snapshot, runs, worker health, sanitized legacy state, quality; cursor and changed flag |
| `/api/runs?after=0&limit=100` | Bounded run list, offset cursor |
| `/api/runs/<UUID>` | One projected run |
| `/api/events?run=<UUID>&after=0&limit=100` | Allowlisted events after a run sequence, next sequence cursor |

All query parameters are validated; duplicate or unknown parameters are rejected.
GET is the only supported method. Host must equal `127.0.0.1:<actual port>`;
Origin, if present, must equal the matching HTTP origin. Cross-site Fetch Metadata
is rejected. No CORS header is returned. Responses disable caching, MIME sniffing,
embedding, forms and external content. Arbitrary files, raw command logs, prompt
text, configuration, exception messages and absolute paths are never resources.

Directory scans stop at 1000 entries, history at 64 files/10000 events, lines at
16KiB, responses at 2MiB, pages at 100 records, and request paths at 2048 characters.
Truncated/unreadable/unknown-version/corrupt history, sequence conflicts/gaps and
partial tails are explicit problems with unknown quality. Exact duplicate events
are ignored and sequence order is restored. Run/project/task identities are separate.
Run-list offsets are snapshot-relative; clients should refresh the list as new runs
appear. A run's sequence cursor is stable and independent of other runs.

Only issue number/state are read from exact legacy issue state filenames (bounded
1MiB JSON). No times/stages/success history are fabricated for older state. Queue
freshness is from the last recorded snapshot, not live GitHub. No heartbeat exists;
unclosed runs are unknown/stale, and an observed worker exit is distinct from run
completion. A successful operation is never inferred from missing evidence.

CI identities are retained exactly from approved metadata; task criteria wording is
withheld with a count. Private evidence must be inspected locally by an operator.
This is a local development observer, not a server intended for network exposure.
