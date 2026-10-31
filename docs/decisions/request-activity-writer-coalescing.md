# Bounded Activity writer coalescing

Collect ordinary data briefly on the existing writer, with a separate urgent
signal for administrative barriers and shutdown. Producers never wait for the
window. Coalesce matching small output/API pieces on the worker, keeping byte
order per request/epoch/representation/invocation and semantic chunk barriers.
Do not assign callback or cross-channel causal meaning to persisted chunk IDs.

Plan each bounded window once, marking prepared singletons so unique channels
do not repeatedly scan the same queued work. Prepared groups recheck scratch
capacity and fall back to original writes if needed, without replanning or
allocating split descriptors. Keep slotted descriptors covered by explicit
metadata credits until release; those credits share active scratch and survive
after buffers drain.

Bound the scan, encoded group size and temporary buffers. Retain every original
reservation until the group is released, and separately reserve merge scratch
against total observer memory. All ingress paths account for that reservation.
Fall back to ordinary writes when merging cannot be admitted; never drop content
merely because scratch is unavailable.

Keep existing short storage operations and physical-budget admission instead of
nesting a bulk transaction around pruning, vacuum or checkpoint calls. Controls
bypass collection delay but cannot interrupt an active SQLite call. Synthetic
write-count/ordering/resource checks do not establish real serving overhead;
high-cardinality CPU, pinned readers, physical limits under sustained load and
real inference remain explicit qualification work.
