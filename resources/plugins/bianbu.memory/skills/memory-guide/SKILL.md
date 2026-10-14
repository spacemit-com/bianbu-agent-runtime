---
schema_version: 2
name: memory-guide
name_zh: 记忆使用指南
name_en: Memory Guide
description: Use durable AgentMemory deliberately and conservatively.
description_zh: 有意识地、克制地使用持久化 AgentMemory。
version: 0.2.0
---

AgentMemory is the default long-term memory backend. Retrieve memory when earlier
preferences, identity, development habits, or decisions are relevant. Proactively
record only stable, useful facts requested or confirmed by the user. Never store
passwords, tokens, private keys, or other secrets.

Use one add or replace operation per AgentMemory call. `memory_retrieve` returns
structured `memory_id`, `kind`, `target`, `slot`, and `revision` fields. Copy those
locators instead of guessing from rendered text. Prefer `memory_id` for one exact
record and prefer `slot` for a correction to a single-valued preference; same-slot
duplicates are reconciled automatically.

Removing a memory requires an explicitly confirmed deletion flow. When text
identifies more than one unrelated record, use the returned candidate list to select
the intended record, then retry with its exact `memory_id`; never guess between
candidates. Tool transport status is not business success: only say that a memory
was recorded, revised, or removed after the JSON result contains `"success": true`.
Do not announce completion before the Tool result arrives.

Do not claim that a separate management interface or authorization method exists
unless the tool result explicitly provides it. If AgentMemory is unavailable, report
that the write did not happen and do not claim success. There is no Runtime-local
long-term memory fallback: when memoryd is unreachable the tools report the failure.
