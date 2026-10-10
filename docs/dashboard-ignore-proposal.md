# Ignore and publication hygiene proposal (not applied)

The existing `.gitignore` and `config.example.json` are unchanged. No real
configuration or active state is modified. The following is a reviewable defense
proposal; filename ignores alone cannot detect secrets embedded in source/history.

```diff
 __pycache__/
 .venv/
 work/
 .ruff_cache/
+.env
+.env.*
+config*.json
+!config.example.json
+state/
+gateway-state/
+events/
+logs/
+*.log
+*.jsonl
+*.tmp
+browser-artifacts/
+.DS_Store
```

Keep external trusted configuration/state as the primary protection. Before applying
this proposal, inspect `git status --short` and `git diff`, confirm which ignored
paths already have tracked files, and preserve the public configuration example.
Do not use `git rm` on existing configuration/state without a separate explicit
instruction. Before any public release, inspect all reachable Git history and
branches for credentials and private files, using a reviewed secret scanner where
available. Do not print discovered secret values; report only paths/object IDs and
perform credential remediation separately. A clean latest checkout does not imply
a clean history.

The cloud session performed a narrow filename/object-only regex check over 53
currently fetched historical blobs (AWS access-key patterns, common GitHub/OpenAI
token patterns and private-key headers) and found no matches. This is heuristic
evidence only, not a guarantee that history is secret-free. Publishing publicly and
changing existing security/configuration settings are outside this implementation.
