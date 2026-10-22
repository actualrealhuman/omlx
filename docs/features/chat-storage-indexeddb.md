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
- Source: local implementation commit `c2ec040fbba636473bd20e3c6ae96adaccff4236` on
  `feature/chat-storage-adoption`.
- Baseline: stable release `v0.7.1`, full commit
  `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- Required changes: the five storage modules in `apps/omlx-web/omlx_web/static/js/`,
  their template includes, and the bounded storage lifecycle, migration, save,
  recovery, delete, clear, import, and stream hooks in `chat.html`. Carry the
  storage tests, real-template Node harness, fake IndexedDB, UI assertions, and
  all locale keys with the implementation.
- Dependencies: no separate runtime feature. Preserve the safety invariant that
  quota, parse, serialization, storage, or migration errors never silently
  delete existing history and require an explicit recovery choice. Keep the
  target's existing partial-stream finalization and composer layout. Theme and
  editor changes that share `chat.html` are not required companions.

The template changes must include queued per-chat writes, revision checks,
explicit record removal with failed-delete retry intent, and guards against
late stream saves and same-ID imports. Do not replace the whole target chat
template from a different feature branch. This branch is prepared on the
stated baseline; it has not yet been qualified on that target.

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
browser storage for any manual migration check. These are required checks, not
claimed results. Integration with only the declared prerequisites has not yet
been qualified; run the suites on this branch before offering it for adoption.

## Limitations and remaining work

Storage is per browser and origin; there is no sync, account service, archive
export, storage meter, or thumbnail generation. Migration retains the legacy
history bytes while converting records. Browser quota and eviction policy still
apply. Runtime tests and a disposable-origin migration check remain pending, so
independent adoption is not yet qualified.
