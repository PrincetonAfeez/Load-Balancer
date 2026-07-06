# Architecture Decision Records

Short records of the non-obvious decisions behind this load balancer. Each
captures the context, the decision, and its consequences (including the
trade-offs we accepted). They complement the narrative in the top-level README.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-asyncio-concurrency-model.md) | asyncio as the single concurrency model | Accepted |
| [0002](0002-layer-4-tcp-core.md) | Layer 4 TCP proxying is the core product | Accepted |
| [0003](0003-sqlite-off-the-hot-path.md) | SQLite persistence kept off the connection hot path | Accepted |
| [0004](0004-rule-dsl-bytecode-vm.md) | A compiled rule DSL on a bounded stack VM (no `eval`) | Accepted |
| [0005](0005-hmac-control-protocol.md) | HMAC-authenticated length-prefixed control protocol | Accepted |
| [0006](0006-transactional-reload.md) | Transactional config reload with rollback | Accepted |
