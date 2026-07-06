# 2. Layer 4 TCP proxying is the core product

Status: Accepted

## Context

Load balancing can be done at Layer 4 (TCP connections) or Layer 7 (per HTTP
request). L7 enables routing by host/path/header but requires parsing and
buffering application protocol data, which couples the hot path to a protocol.

## Decision

The core is an L4 TCP proxy: accept a client, choose a backend, connect, and
relay raw bytes in both directions until either side closes, errors, or times
out. The hot path stays protocol-agnostic. Any L7 mode would be a separate
layer on top, not a rewrite of the core (and is out of scope here).

## Consequences

- The relay is a pair of byte pumps plus an idle watcher; it never inspects or
  buffers application data, keeping latency and memory predictable.
- Sticky routing is by source IP (consistent hashing), since there is no
  request key to hash at L4.
- Trade-off: no per-request balancing, header/cookie routing, or HTTP health
  checks. These are explicitly out of scope, not missing features.
