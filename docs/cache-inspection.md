# Cache inspection files

Cache inspection is an optional companion to the main paged SSD cache. It saves
exact token IDs and an annotated text view next to each persisted KV block:

```text
cache/a/
  abcd….safetensors
  abcd….tokens
  abcd….txt
```

It is disabled by default. Enable **Save Cache Inspection Files** in the admin
dashboard's advanced cache settings, use `omlx serve --cache-inspection`, set
`OMLX_CACHE_INSPECTION=true`, or set `cache.cache_inspection` to `true` in
`settings.json`. The admin settings API accepts `cache_inspection: true`.
Runtime changes use the existing cache-settings reload mechanism.

**Privacy:** both IDs and text expose recoverable prompt content, including
system instructions, tool results, and any generated tokens incorporated into
the cached sequence. Inspection files are created with owner-only permissions.
Keep the cache directory private and exclude it from unintended backups or
sharing. Turning capture off stops new capture; already captured files remain
until their KV block is evicted or the cache is cleared. Filesystem deletion is
not a guarantee of secure erasure from SSDs, snapshots, or backups.

## What is captured

Each sidecar describes the token segment associated with its KV block, not an
entire request or a transcript. Only tokens actually covered by a stored block
are included. Ordinary cache storage omits trailing partial blocks; existing
exact-prefix storage can retain a partial terminal block. Some recurrent or
rotating layers contain a boundary state rather than one tensor row per token.

The `.tokens` file is UTF-8 JSON:

| Field | Meaning |
| --- | --- |
| `format_version` | Inspection schema version, currently `1` |
| `renderer_version` | Text renderer version, currently `1` |
| `block_hash`, `parent_hash` | Current block and preceding block; root parent is `null` |
| `model` | Model identifier supplied by the scheduler |
| `tokenizer` | Tokenizer class and a SHA-256 fingerprint of the serialized backend when available |
| `token_start`, `token_count` | Absolute token offset and number of IDs in this block |
| `token_ids` | Exact ordered integer IDs |
| `media` | Optional factual descriptors from preprocessing |

Parent links support inspection of available prefix chains. Earlier blocks may
have been evicted, so a chain need not be complete. IDs and metadata alone are
not sufficient to recreate multimodal input, images, or model state. These
files are never consumed by KV restoration or used to change cache keys.

The `.txt` file has a short header and a lossy decoded view. It preserves known
special-token spellings, decodes ordinary text in contiguous runs, and
compresses repeated registered image/audio/video markers into counted
annotations. Unknown or undecodable IDs get explicit annotations. Replacement
characters may appear where a block splits a character. Control characters
(except newline and tab) and literal annotation delimiters are escaped to make
terminal inspection safe. `⟦…⟧` denotes an oMLX annotation, not model text.

## Media and cost

Image descriptors reuse loaded image dimensions and existing cumulative image
hashes. Their `key_start` and `key_end` describe cache-key context clipped to
this block. They are **not image-token boundaries**; `token_span` is `null`.
Later images are excluded from shared earlier blocks. There are no source
URLs, filenames, request IDs, pixel copies, or additional image hashes.

No OCR, captioning, additional model inference, media decoding, or network
requests are performed. Audio/video markers can be rendered, but this version
does not extract additional audio/video metadata. A future renderer can make
better use of saved metadata; semantic image descriptions would generally
require the original image or another suitable retained representation.

An independent CPU tokenizer snapshot is created once for an enabled model.
Text decoding and file writes normally run on the existing SSD worker, outside
MLX buffer locks. The existing queue-pressure fallback can write inline;
capture therefore has measurable CPU, filesystem, and memory costs. Complete
sidecars are not rendered or rewritten on cache hits. Missing sidecars are
backfilled opportunistically when tokens pass through prefix storage again;
backfill never rewrites tensors and is dropped if the write queue is full.

## Persistence and failure behavior

Sidecars follow tensor persistence timing. Write-back hot-cache entries retain
their inspection data until they reach SSD. Write-through persists both.
RAM-only mode never creates inspection files. SSD accounting includes sidecar
bytes, including files created before capture was disabled.

Each file is replaced atomically, but the three-file set is not a transaction.
A crash or disk error can leave an incomplete set. Sidecar failures are logged
without prompt content and do not invalidate usable tensors or fail inference.
Existing caches without sidecars remain readable, as do caches with corrupt
inspection files. Presence is checked when indexing; this is not a continuous
sidecar integrity monitor.

Eviction and clearing remove sidecars with their KV block. Startup and clear
remove recognized orphan sidecars and interrupted inspection temporary files;
unrelated files are left alone. The existing single-server cache ownership
model is unchanged; this feature does not add coordination between separate
server processes writing the same cache directory.

SSD statistics expose `inspection_writes`, `inspection_errors`, and
`inspection_backfill_drops`. File counts continue to count cache blocks; byte
totals include their companion files.

Distributed prompt snapshots, separate vision-feature caches, speculative draft
caches, media retention, and a cache-content viewer are outside this version.

## Validation

```bash
python -m pytest tests/test_inspection.py tests/test_inspection_files.py \
  tests/test_cache_inspection_integration.py
python benchmarks/bench_cache_inspection.py --blocks 64 --repeats 3
```

The benchmark uses synthetic blocks and a local synthetic tokenizer, with no
model download. It reports producer time, total persistence time, and file
bytes for disabled/enabled capture. These are filesystem microbenchmarks, not
inference throughput measurements.
