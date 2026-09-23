# Technical decision records

This directory stores durable technical decisions that future contributors and
agents may need to understand. These records are public documentation.

Create or update a record when a change establishes a consequential invariant,
chooses between credible architectures, separates work into intentional phases,
or reverses an earlier decision. Routine implementation detail, mutable branch
status, agent ownership, and local operational state do not belong here.

Each record should include:

- status and decision date;
- context and constraints;
- the decision;
- consequences and known follow-up work;
- links to relevant code, tests, or superseded records.

Use sequential filenames such as `0001-short-title.md`. Mark superseded records
instead of silently rewriting their historical decision. Keep every record
portable and publication-safe: no local usernames, absolute home paths, machine
names, private addresses, credentials, unpublished artifact locations, or
private conversation excerpts.
