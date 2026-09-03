# Agent Notes

English | [中文](README.zh.md)

An **Agent Note** records a durable decision or proposal that affects Artoo: the rationale, alternatives, consequences, and verification that code and API documentation should not carry. This directory follows the DeepSeek Harness Agent Note conventions, adapted to Artoo's Python/React stack.

## Layout and naming

Every note has two axes encoded in its path:

```text
{lifecycle}/{class}/yyyy-mm-dd-topic-title.md
```

The lifecycle is the note's status:

- `proposed/` — reviewed before implementation; the work is not fully shipped.
- `implemented/` — the decision has shipped and the note remains the current owner of its rationale.
- `rejected/` — the proposal was considered and declined.
- `archived/` — frozen low-future-value implemented records. Archived files are historical snapshots and must not be edited or treated as current authority.

The class is one closed set:

| Class | Covers |
|---|---|
| `feature` | A new user- or model-facing capability. |
| `bug-fix` | Corrects a defect or closes a gap surfaced by an incident. |
| `simplification` | Removes code, behavior, or surface area without adding a capability. |
| `architecture` | A structural decision about shipped source, runtime vocabulary, persistence, or wire contracts. |
| `process` | Tooling, workflow, release, or repository policy around the code. |
| `testing` | Test infrastructure and verification strategy. |

The filename date is when the topic was first proposed. Titles are lowercase, hyphenated, and descriptive. Each English note has a Chinese counterpart with the same base name plus `.zh.md`; cross-notes use relative Markdown links.

Do not add a central `INDEX.md`. Browse or search the lifecycle tree; active records are the working inventory.

## When a note is required

Every non-trivial change adds or updates at least one Agent Note in the same PR. Non-trivial includes runtime behavior, architecture, backend/frontend contracts, the Open API, SSE, persisted events, model/provider integration, database or migration behavior, configuration, deployment, testing strategy, or another decision a maintainer may revisit.

Updating the existing owner of a decision is preferred; do not create a duplicate. A note is never rewritten into a different decision: create a new note and cross-link it. Implemented notes may update facts in place when code paths, names, defaults, or tests change, but the decision and rationale remain stable unless superseded.

## Implemented note format

The first lines are exact and stay English in both language files:

```markdown
# Agent Note: <title>

Status: implemented
```

For a proposed note, use `Status: proposed`. For a rejected note, use `Status: rejected — <one-line reason>`. The status value must agree with the lifecycle folder.

An implemented note uses this body skeleton:

```markdown
## Problem
## Decision
## Alternatives considered
## Consequences
```

Bespoke technical sections may sit between `Decision` and `Alternatives considered`. A present-tense `Testing` section is allowed. Implemented notes must not use proposal-era headings such as `Proposal`, `Plan`, or `Acceptance criteria`.

`Alternatives considered` is mandatory. Record each genuine alternative and why it lost; use one bold-led paragraph per alternative. Do not invent alternatives that were not actually considered.

`Consequences` records both costs and benefits, including compatibility impact, operational burden, ownership boundaries, and known gaps.

## Chinese counterpart

Each `.md` file has a `.zh.md` counterpart with section-for-section parity. The header `# Agent Note: ` and `Status:` tokens stay in English. Technical identifiers, event names, file paths, and commands stay verbatim. Both languages carry equal authority.

Each bilingual pair has a sidecar named after the English file with `.i18n.yaml`. The sidecar records the Git blob hash of each language file at the last confirmed-consistent state. When either side changes, update the other side in the same change and re-record both hashes.
