# 6. Transactional config reload with rollback

Status: Accepted

## Context

Operators change backends, weights, strategy, timeouts, health settings, and
routing rules without restarting. A reload that applies a half-valid config, or
that drops in-flight connections, would be worse than no reload at all.

## Decision

Reload is validate-then-swap. The candidate TOML is parsed and fully validated
(addresses, weights, thresholds, strategy, unknown sections, and the compiled
rule), the in-memory pool/proxy/health state is updated from the validated
candidate, and only then is the accepted snapshot persisted to SQLite. Any
failure before the swap leaves the previous good config running and does not
record a rejected candidate in the database. Listener/control addresses and the
metrics database path/queue size are restart-only and rejected on reload.

## Consequences

- The snapshot is written *after* the in-memory swap so a rejected reload never
  leaves a misleading row in `config_snapshots`. The swap itself operates only
  on already-validated data; SQLite persistence is the final step.
- Removed backends enter `draining` and keep serving existing connections until
  their active count reaches zero; they are then pruned.
- Strategy state is preserved across reloads, except a cached consistent-hash
  ring is rebuilt when `virtual_nodes_per_weight` changes so the new value
  actually takes effect.
- Graceful shutdown acquires the reload lock so an in-flight reload cannot
  overlap with proxy drain/teardown.
- Trade-off: runtime admin edits (via the control socket) are in-memory only;
  a reload makes the TOML file authoritative again.
