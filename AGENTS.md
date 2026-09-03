# AGENTS.md

Artoo is a retrieval-grounded assistant platform with a Python/FastAPI backend and a React frontend. Read the affected module documentation before changing backend, frontend, retrieval, or deployment code.

## Agent Notes

**Every non-trivial change MUST add or update an Agent Note in the same PR.** A change is non-trivial when it affects runtime behavior, architecture, a backend/frontend contract, the Open API, SSE or persisted event structure, configuration, storage, model/provider integration, testing strategy, or a decision a maintainer may need to revisit. Only purely mechanical or local edits are exempt.

Agent Notes live under [`.agents/notes`](.agents/notes/README.md) and follow the DSH-style lifecycle and naming rules:

```text
.agents/notes/{lifecycle}/{class}/yyyy-mm-dd-topic-title.md
```

- Lifecycle: `proposed`, `implemented`, `rejected`, or `archived`.
- Class: `feature`, `bug-fix`, `simplification`, `architecture`, `process`, or `testing`.
- Every note has an English file and a `.zh.md` counterpart with the same name except for the suffix.
- The filename date is the date the topic was first proposed.
- Implemented notes stay synchronized with the shipped behavior in the same change; decisions are superseded by a new linked note, not silently rewritten.

See [`.agents/notes/README.md`](.agents/notes/README.md) for the complete format and [`.agents/notes/implemented/AGENTS.md`](.agents/notes/implemented/AGENTS.md) for shipped-record rules.

## Verification

Run the narrowest relevant checks for the changed surface: backend tests under `backend/`, frontend build/tests under `frontend/`, Open API examples for protocol changes, and deployment validation for compose changes. Report only the commands and outcomes actually run. Do not commit credentials, local reference repositories, logs, build artifacts, or ignored local test fixtures.
