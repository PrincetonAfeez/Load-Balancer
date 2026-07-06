# 5. HMAC-authenticated length-prefixed control protocol

Status: Accepted

## Context

The CLI manages a running daemon (status, stop, reload, drain, strategy, backend
edits) over a local socket. Even a localhost socket is reachable by any local
user, so admin commands need authentication and tamper/replay resistance —
without pulling in TLS/PKI, which is out of scope.

## Decision

A length-prefixed JSON frame protocol: a 4-byte big-endian length followed by a
UTF-8 JSON payload of `version`, `timestamp`, `nonce`, `command`, `args`, and a
`signature`. The signature is `HMAC-SHA256` over the canonical JSON (sorted keys,
compact separators, signature excluded), verified with `hmac.compare_digest`.
The server rejects oversized/malformed frames, unsupported versions, expired
timestamps, replayed nonces (bounded replay cache), unknown commands, and bad
argument types.

Responses use the same HMAC scheme over `version`, `timestamp`, `nonce`, `ok`,
and either `data` or `error`. The CLI verifies every response signature and
tracks response nonces in a bounded cache to reject replays within the clock-skew
window.

## Consequences

- Tamper, expiry, and replay are rejected on both requests and responses;
  request signatures are verified *before* the nonce is consumed, so forged
  frames cannot exhaust the request cache.
- Reads are bounded by a frame-size cap and a per-connection read timeout;
  oversized responses return a structured error instead of a truncated frame.
- The secret is generated with `secrets.token_urlsafe`, stored outside source
  control, and written `0600` where the OS honors it.
- Trade-off: this authenticates and integrity-checks traffic but does not encrypt
  the channel. Honest posture: localhost is not itself an auth boundary on a
  shared machine; this is educational crypto, not production PKI.
