# Agent Note: Adopt DSH-style Agent Notes

Status: implemented

English | [中文](2026-09-03-adopt-agent-notes.zh.md)

## Problem

Artoo's repository history explained many implementation details, but not consistently why a contract changed, which alternatives were rejected, which compatibility cost was accepted, or how the decision should be verified. Agent-led changes were especially exposed to this: the reasoning stayed in chat history and disappeared before the next maintainer needed it.

## Decision

Artoo uses DSH-style Agent Notes under `.agents/notes`. Notes are path-encoded as `{lifecycle}/{class}/yyyy-mm-dd-topic-title.md`, with `proposed`, `implemented`, `rejected`, and `archived` lifecycles; `feature`, `bug-fix`, `simplification`, `architecture`, `process`, and `testing` classes; and English plus Chinese counterparts for every active note.

Every non-trivial PR must add or update the note that owns the affected decision. Implemented notes use `Problem`, `Decision`, `Alternatives considered`, and `Consequences`; they may add a present-tense `Testing` section. Each bilingual pair has an `.i18n.yaml` sidecar recording both Git blob hashes at the last confirmed-consistent state. Root `AGENTS.md` makes the rule discoverable to future agents.

## Alternatives considered

**Continue relying on commit bodies.** Rejected. Commit bodies are useful for a change, but they are awkward to update when facts move and do not provide a searchable inventory of active decisions.

**Use only the Open API document.** Rejected. It records the current contract, not why competing designs were rejected or which trade-offs are intentional.

**Create an ad hoc decision log.** Rejected. A free-form log would drift from the implementation and would not distinguish shipped, proposed, rejected, and historical records.

## Consequences

Future agents and maintainers can find the owner of a contract decision, read its rejected alternatives, and see the verification evidence without reconstructing chat history. Bilingual sidecars make it obvious when one language version has drifted.

The cost is an additional documentation step and the responsibility to update a note in the same PR. Artoo does not yet run an automated note-format or translation-pairing gate, so adherence is currently enforced by review and repository instructions.
