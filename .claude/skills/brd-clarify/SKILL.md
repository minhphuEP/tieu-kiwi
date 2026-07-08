---
name: brd-clarify
description: This skill should be used when the user asks to "clarify a BRD", "clarify requirements", "resolve requirement ambiguities", or "interview the PO", or when a BRD/PRD has gaps, undefined behaviour, or conflicting requirements before planning begins.
model: sonnet
argument-hint: "[path/to/brd.md or paste BRD text]"
allowed-tools:
  - Read
  - Write
  - Agent
  - AskUserQuestion
---

You clarify a BRD by identifying ambiguities and interviewing the PO to resolve them.
Output is a clarified requirements block appended to the BRD file (or saved as a new file if BRD was pasted as text).

## Input

1. File path to a BRD document — read and analyse it.
2. Raw BRD text pasted inline — treat it as the BRD content.
3. No argument — ask the user to provide the BRD file path or paste the content.

## Workflow

### Step 1: Read and understand the BRD

If a file path was provided, read the file. If raw text, use it directly.

Summarise what is being built in 2–3 sentences. Identify the feature name and target users.

### Step 2: Identify ambiguities

Analyse the BRD for gaps across these dimensions. Flag only genuine gaps — do not manufacture ambiguities for a well-specified BRD.

**Scope and ownership**
- Who initiates or triggers the feature?
- Who are the affected users (all, a subset, specific roles)?
- Which system owns the business logic or output?

**Behaviour and edge cases**
- What happens when required data is missing or invalid?
- Are there undefined states (empty, loading, error)?
- Are success and failure paths both described?

**Constraints**
- Is there a latency or performance requirement?
- Are there compliance, data residency, or security constraints?
- Is a feature flag or phased rollout expected?

**Conflicts**
- Do any requirements contradict each other?
- Are priorities between requirements stated?

If fewer than 3 ambiguities are found, note them and proceed. If none are found, skip Steps 3–4, output "BRD is sufficiently specified — no clarification needed.", and stop.

### Step 3: Interview the PO

Launch the `brd-clarifier` agent via the `Agent` tool (`subagent_type: brd-clarifier`). Pass:
- The BRD content (full text or a concise summary preserving all relevant context)
- The ambiguities list (structured, grouped by dimension)

Wait for the agent to return the clarified requirements block.

### Step 4: Append clarified requirements

If the BRD was read from a file:
- Append the clarified requirements block to the end of that file.
- Confirm to the user: "Clarified requirements appended to [file path]."

If the BRD was pasted as text:
- Save the full BRD + clarified requirements block to `docs/brd-clarified.md`.
- Confirm to the user: "Saved to docs/brd-clarified.md."

If there are Open Items (TBD answers from the PO):
- List them explicitly and tell the user: "These items need follow-up before planning begins."
