---
title: Keep router ABI locks out of host callbacks
author: Aw338WoWmUI
date: 2026-10-17
branch: main
touches:
  - ".github/workflows/build.yml"
  - Makefile
  - README.md
  - abi.go
  - abi_test.go
  - main.go
  - registry.json
  - scripts/test_abi_concurrency.py
---

## Decided
Fork the existing router as a drop-in fix rather than rewriting it. Snapshot the host pointer under the ABI state lock, release the lock before host callbacks, and use the existing ABI-call and asynchronous-stream in-flight counts to protect host lifetime through response buffer freeing.

## Tried and ruled out
The compiled C ABI test holds one host.model.stream_read callback open and starts another real executor stream. Upstream v0.1.2 blocks the second stream until the first is released. With the lock narrowed, the independent stream and reconfiguration finish while the first remains blocked; shutdown still drains callbacks and buffers. Testing only mocked host callbacks or single-request fallback did not reach this lock.

## Watch out
Keep plugin ID and config unchanged, but publish fork metadata and version separately so a store update cannot silently replace the deployed fix. Every host callback must remain within an in-flight ABI call or tracked stream worker; new untracked background callbacks would invalidate the lifetime guarantee. Preserve upstream MIT attribution. Do not hold the state lock over any network wait.
