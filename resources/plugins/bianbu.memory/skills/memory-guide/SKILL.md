---
schema_version: 2
name: memory-guide
name_zh: 记忆使用指南
name_en: Memory Guide
description: Use durable AgentMemory deliberately and conservatively.
description_zh: 有意识地、克制地使用持久化 AgentMemory。
version: 0.4.0
---

AgentMemory is the default long-term memory backend. Retrieve memory when earlier
preferences, identity, development habits, or decisions are relevant. Proactively
record only stable, useful facts requested or confirmed by the user. Never store
passwords, tokens, private keys, or other secrets.

Use one add or replace operation per AgentMemory call. `memory_retrieve` returns
structured `memory_id`, `kind`, `target`, `slot`, `revision`, and `tags` fields.
Copy those locators instead of guessing from rendered text. Prefer `memory_id` for
one exact record and prefer `slot` for a correction to a single-valued preference;
same-slot duplicates are reconciled automatically.

The user-designated important mark is a record attribute, not a content
category. When the user asks for a memory to be marked important, kept, or
pinned, call `memory` with `action: "mark_important"` and the record's exact
`memory_id`; clear it with `unmark_important`. It accepts a record of any kind
and does not widen what the account uploads by itself. Never express the mark as
text: a prefix such as `【重要】` is ordinary content and changes nothing. The
mark is kept as the reserved tag `important`, so `memory_retrieve` hands it back
under each entry's `tags` — read it there to answer which memories are marked,
instead of guessing from the content. Confirm the write itself from the `memory`
result (`"important": true`), and never tell the user the action does not exist
unless the tool actually returns `invalid_operation`.

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
