# Chat storage: IndexedDB records and recovery

Feature ID: `chat-storage-indexeddb`

## Behavior and use

The built-in Chat saves conversations as per-chat records. It chooses IndexedDB
when available and falls back to localStorage records when IndexedDB is
unavailable. Existing whole-history localStorage data is migrated before the
record list is shown. Revision checks detect concurrent edits from another tab.
The full original image and file attachments remain available after reload.

This runs automatically when Chat starts; there is no setting to enable. Chat
history remains local to the browser and origin. For example, opening an
existing chat in a second tab gives both tabs their own copy; a conflicting save
stops and offers the user a choice rather than overwriting either copy.

## Source and adoption boundary

- Availability: unpublished local feature branch; no public source is available.
- Source: `feature/chat-storage-adoption`, beginning with implementation commit
  `c2ec040fbba636473bd20e3c6ae96adaccff4236` and including focused fixes at
  tested revision `8afa2b68b30c10037469ec501360baf5cf971349`.
- Baseline: stable release `v0.7.1`, full commit
  `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- Required production changes: the storage modules
  `apps/omlx-web/omlx_web/static/js/chat_history_storage.js`,
  `apps/omlx-web/omlx_web/static/js/chat_record_store.js`,
  `apps/omlx-web/omlx_web/static/js/chat_indexeddb_store.js`,
  `apps/omlx-web/omlx_web/static/js/chat_history_migration.js`, and
  `apps/omlx-web/omlx_web/static/js/chat_media_store.js`; their includes and the
  bounded storage lifecycle, migration, save, recovery, delete, clear, import,
  and stream hooks in `apps/omlx-web/omlx_web/templates/chat.html`, including
  initialized write queues/deletion tombstones and a discarded-stream finalizer
  guard. Carry the storage tests, real-template Node harness, fake IndexedDB, UI
  assertions, and all locale keys with the implementation.
- Dependencies: no separate runtime feature. Preserve the safety invariant that
  quota, parse, serialization, storage, or migration errors never silently
  delete existing history and require an explicit recovery choice. Keep the
  target's partial-stream output/error behavior and one-field composer layout.
  Theme and editor changes that share `chat.html` are not required companions.

The template changes must include queued per-chat writes, revision checks,
explicit record removal with failed-delete retry intent, and guards against
late stream saves and same-ID imports. Do not replace the whole target chat
template from a different feature branch. This branch is prepared on the
stated baseline. The focused synthetic suites below pass on the prepared port;
browser migration and wider target qualification remain pending.

## Validation

Run the focused suites:

- `node --test apps/omlx-web/tests/chat_history_storage.test.cjs apps/omlx-web/tests/chat_record_store.test.cjs apps/omlx-web/tests/chat_indexeddb_store.test.cjs`
- `node --test apps/omlx-web/tests/chat_history_migration.test.cjs apps/omlx-web/tests/chat_backend_migration.test.cjs apps/omlx-web/tests/chat_media_store.test.cjs`
- `node --test apps/omlx-web/tests/chat_app_integration.test.cjs`
- `python3 -m pytest -q apps/omlx-web/tests/test_chat_ui_overhaul.py`

These cover localStorage fallback, migration retry, attachment preservation,
revision conflicts, visible write failures, deletion and clear persistence,
failed-delete recovery, stale stream saves, same-ID import, variant write
serialization, and partial output after stop or stream error. Use disposable
browser storage for any manual migration check. Local validation on 2026-10-22
UTC at implementation revision `8afa2b68b30c10037469ec501360baf5cf971349`:

- Storage, record, and IndexedDB Node suites: 66 passed.
- Migration and media Node suites: 60 passed.
- Real-template app integration Node suite: 40 passed.
- Chat UI Python suite: 19 passed.
- `git diff --check`: passed.

These synthetic suites use fake browser storage and do not establish real-browser
behavior. A disposable-origin migration check, broader browser qualification,
performance checks, and packaged-app qualification remain pending.

## Limitations and remaining work

Storage is per browser and origin; there is no sync, account service, archive
export, storage meter, or thumbnail generation. Migration retains the legacy
history bytes while converting records. Browser quota and eviction policy still
apply. Independent adoption is not yet fully qualified while disposable-origin
migration and broader browser checks remain pending.

Prepared branch: `feature/chat-storage-adoption`, tip `f9ec698a11e6d6086be28141134ee2a97898d66b`
(implementation plus recipe updates).

Return to the [catalog](../FORK_FEATURES.md) or
[composition guide](../FEATURE_COMPOSITION.md).

The prepared tip includes a documentation-only update dating the source-owned
publication-review note. The implementation and recorded test revisions above
are unchanged; verify the remote before treating this source as public.
