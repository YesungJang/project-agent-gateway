# Browser Dashboard

The root URL shows a CodePipeline-style grouped pipeline, recorded queue, run
selection, separate worker health and historical run status, task attempts/repairs,
fixed diagnostic summaries, PR head/base, exact CI identities and merge confirmation.
Labels and symbols accompany colors. Run selection is the only button interaction.
The page supports keyboard focus and narrow screens. No external assets are loaded.
All API-derived text uses DOM text nodes, not HTML interpretation.

Polling runs every five seconds with a four-second request timeout. API errors leave
the last view visible with an explicit stale/offline warning and last successful API
observation time. Event/queue ages continue advancing in the browser. An API connection
does not establish worker liveness or refresh the GitHub queue.

Actual task criteria wording is withheld; the task panel gives its count and directs
operators to private state. This is a deliberate prompt-privacy limitation of the MVP.

To view synthetic scenarios without executing a worker or contacting services:

```sh
python3 demo_dashboard.py --output /tmp/gateway-dashboard-demo-NEW
python3 dashboard.py --state /tmp/gateway-dashboard-demo-NEW --port 8765
```

Open `http://127.0.0.1:8765`. The generator refuses any existing directory and any
output inside the checkout. Demo history is synthetic, uses `example/gateway` and
must never be mixed into real Gateway state. Scenarios include completed, repaired,
repair limit, ERROR/RISK, bootstrap failure, CI pending/failure, crash, partial tail,
FAST and DEBATE. It intentionally includes old/stale snapshots and an incomplete
history warning. Ctrl-C stops only the observer.

Optional browser verification, if Playwright is already installed in your development
environment (not a runtime requirement): `node tests/browser_dashboard.cjs`, while
the synthetic observer is serving port 8765. `DASHBOARD_TEST_URL` selects another
loopback port and `DASHBOARD_TEST_ARTIFACTS` selects a screenshot output directory.
The app and Python tests do not require Playwright or any added package.

The cloud implementation session could not run a real browser: bundled Playwright
had no Chromium executable, and the separate cloud browser could not reach the
development process's isolated localhost. Layout, actual keyboard navigation and
mobile rendering therefore remain unverified. A dependency-free Node DOM boundary
test checks rendering/selection, XSS-as-text and offline warnings, but does not claim
browser equivalence: export `Reader(...).snapshot()` as JSON and run
`node tests/dashboard_dom.cjs /path/to/snapshot.json` on the 12-scenario demo.
