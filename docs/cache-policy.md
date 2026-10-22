# Per-request cache policy

Generation requests can choose how oMLX retains reusable KV state and image
features. This oMLX extension applies to `/v1/completions`,
`/v1/chat/completions`, `/v1/responses`, and `/v1/messages`, for both streaming
and non-streaming requests. Requests that omit a policy keep the server's
existing behavior.

The JSON `cache_policy` object has independent `kv` and `images` components.
Each accepts `default`, `memory`, or `none`:

| Value | Behavior |
| --- | --- |
| `default` | Follow the server's configured cache behavior. |
| `memory` | Allow reuse and best-effort RAM retention within the existing budget; do not grant disk-persistence permission. |
| `none` | Do not retain new reusable entries for that component in RAM or on disk. Active inference still uses request-local buffers. |

For example, this keeps reusable KV state in RAM when capacity allows, while
avoiding reusable image-feature entries:

```json
{
  "model": "my-model",
  "messages": [{"role": "user", "content": "Describe this image."}],
  "cache_policy": {"kv": "memory", "images": "none"}
}
```

Clients that cannot add JSON fields can set either component with request
headers. Body and header choices can be combined, but an explicit body value
and its corresponding header must agree. Omitted components retain their own
`default` behavior.

```http
X-oMLX-KV-Cache: memory
X-oMLX-Image-Cache: none
```

The example above is useful for one-shot image descriptions, titles, or
metadata. To make the request transient across both reusable caches, set both
components to `none`. Setting only `images` to `none` does not prevent
image-derived state from remaining in the request's KV cache.

Every successful participating response includes both policy acknowledgement
headers, including before a streaming response begins:

```http
X-oMLX-KV-Cache: memory
X-oMLX-Image-Cache: none
```

An acknowledgement of `none` confirms the no-new-retention choice. `disabled`
means that cache subsystem is unavailable or disabled on the server; it
trivially satisfies a restrictive request. `default` and `memory` describe the
effective server/runtime tier. These values do not promise a cache hit or a
memory reservation. A `memory` choice remains best-effort when the budget is
zero, full, or cannot fit an entry. The server never enables a cache or adds a
budget to satisfy a request. Browser clients can read both response headers
through the API's CORS exposed-header list.

The first implementation supports the local batched text and VLM engines.
Other engine families reject explicit restrictive choices they cannot honor,
before request preprocessing or inference. Check the response acknowledgement
when relying on this extension; older oMLX versions may ignore it.

Policies control new reusable cache retention. They do not purge existing
entries, cancel writes already authorized by another request, or revoke an
existing persistence grant. Compatible entries can still be read, so a
memory-only request may reuse an existing SSD prefix. A default request may
authorize persistence of a shared entry only when it actually adopts that
compatible entry for reuse or creates it. A later memory-only request does not
revoke that permission. This preserves cache sharing; it does not provide
request isolation or secure erasure.

`kv` covers target and draft KV state, recurrent state, and related checkpoints.
`images` covers the separate image-feature cache, including supported video
features that use it. Each request sets its own policy; conversation or
response IDs do not carry policy forward. This setting is independent of
conversation storage, Responses `store`, logs, usage records, media temporary
files, model files, compiler caches, and OS swap. In particular, `store: false`
does not control KV persistence, and this policy does not prevent all disk
activity associated with a request.
