# 0001: Preserve chat history on browser-storage failure

- Status: Accepted
- Decision date: 2026-09-22

## Context

The web chat stored the complete history as one browser `localStorage` value.
Normal saves enforced a fixed chat-count cap, and quota recovery repeatedly
removed older entries until the write succeeded. Malformed stored JSON could
also be treated as an empty history and later overwritten. These behaviors made
browser-storage pressure or corruption appear as unexplained data loss.

Moving immediately to a different storage system would have combined the
urgent data-loss fix with a larger asynchronous migration and multi-tab design.

## Decision

The immediate safety change is intentionally independent of a future storage
migration:

- Never delete existing chats automatically to satisfy a count limit or
  browser quota.
- Build candidate history separately and preserve the last committed raw bytes
  if reading, serialization, or persistence fails.
- Preserve malformed stored data for export and recovery rather than replacing
  it with an empty history.
- Block unsafe chat mutations while persistence is unavailable, while keeping
  explicit backup, retry, history management, deletion, and clear-history
  recovery actions available.
- Keep imports all-or-nothing.
- Treat an assistant response that cannot be persisted as recoverable unsaved
  content rather than claiming that it was saved.

An IndexedDB design, using one record per chat, is separate follow-up work. Its
migration must validate the existing value, write all records transactionally,
verify the committed result, retain the legacy value as a backup, and remove
that backup only through an explicit user action.

## Consequences

- Storage exhaustion becomes visible and recoverable instead of destructive.
- Users, not quota-recovery code, decide when chat history is deleted.
- The focused safety fix remains reviewable independently of an IndexedDB
  migration.
- Until that later migration, whole-history `localStorage` writes and weak
  multi-tab coordination remain scalability limitations.

The implemented safety behavior is covered by
`tests/chat_history_storage.test.cjs` and `tests/test_chat_ui_overhaul.py`.
