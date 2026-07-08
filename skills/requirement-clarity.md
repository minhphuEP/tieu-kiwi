---
name: requirement-clarity
description: Reviews a requirement/BRD/PRD/Jira story for ambiguity, undefined edge cases, and untestable acceptance criteria. Use before test-case writing begins, when critiquing a PRD/Design/spec, or when a story is marked ready for QE.
---

# Requirement Clarity

## Overview

A requirement is ready for QE only when every acceptance criterion is unambiguous and testable. Flag genuine gaps against the three dimensions below — do not manufacture problems in a well-specified section. A requirement with zero findings across all three dimensions is sufficiently specified; say so and stop.

## When to Use

- Critiquing a PRD/Design/spec before test-case writing begins
- Assessing whether a Jira story is ready for QE ("Beta Ready", "Ready for Dev")
- Reviewing acceptance criteria for testability
- A BRD/PRD has gaps, undefined behaviour, or conflicting requirements before planning begins

## The Three Ambiguity Dimensions

### 1. Behaviour and Edge Cases

- What happens when required data is missing or invalid?
- Are there undefined states (empty, loading, error, partial failure)?
- Are success and failure paths both described?
- Is corner-case numbering complete (no gaps like CC1 → CC3 with no CC2)?
- Are exact copy strings (toasts, empty states, errors) specified, not paraphrased?

### 2. Constraints

- Is there a latency or performance requirement?
- Are there compliance, data residency, or security constraints?
- Is a feature flag or phased rollout expected, and is it explicitly gated?
- Are timezone/locale-dependent values (e.g. "today + 30 days") anchored to a specific clock?

### 3. Conflicts

- Do any requirements contradict each other?
- Are priorities between requirements stated?
- Does a described future-state behavior ("will change to X when Y model ships") leave the current-scope behavior ambiguous?

## Top 3 PO Questions

The single highest-value question per dimension — ask these first when a gap is genuinely unresolved:

1. "What should happen if the required data is missing or incomplete?" (Behaviour)
2. "Should this be behind a feature flag for a phased rollout?" (Constraints)
3. "Which of these two requirements should win if they conflict?" (Conflicts)

## Untestable AC Patterns

| Pattern | Why It Blocks Testing | Fix |
|---|---|---|
| Field/action mentioned with no validation rule | Can't assert required vs. optional, enabled vs. disabled | State the exact validation and resulting UI state |
| Named field with no definition of its data source | Tester can't verify against real data | Define the field, its source, and its edge values (empty, zero, max) |
| Mode/segmented control with only one branch specified | The unspecified branch is entirely untestable | Add ACs for every branch, not just the default |
| Corner case numbering has a gap | A distinct failure mode was likely dropped silently | PO confirms removal or fills the gap |
| Copy described narratively ("shows an error") instead of verbatim | Tester can't assert exact string/interpolation | Quote the exact string including variables |
| Story marked ready-for-test with no ACs at all | Nothing to trace pass/fail against | Block until ACs are attached; do not accept metadata-only tickets |

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The design shows it, so it doesn't need to be written down" | Designs show the happy path; QE needs the failure paths and validation rules in text too |
| "We'll figure out the edge case during dev" | Undefined edge cases get implemented inconsistently and ship untested |
| "It's obvious what should happen" | If it were obvious, two engineers wouldn't implement it two different ways — write it down |
| "This is a small story, it doesn't need full ACs" | Story size doesn't correlate with ambiguity; a 1-point story can still be untestable |
| "The PRD covers it, the ticket doesn't need to repeat it" | The ticket is what QE tests against — link the exact PRD section, don't assume it'll be read |

## Red Flags

- Ticket has a status like "Beta Ready" or "Ready for Dev" but no description or ACs
- Two or more distinct features bundled into one story with no scope split
- Segmented control / mode switch with ACs for only one mode
- Corner-case list has a numbering gap
- Toast/error copy paraphrased instead of quoted verbatim
- A field is copied/reset on some operation with no explicit inherited-vs-reset list
- A future-state behavior is mentioned with no flag or version gate for the current scope

## Turning Findings into PO Questions

Each finding in this rubric maps to a question the PO must answer, not just a complaint. When flagging a gap, phrase the fix as the direct question (see "Top 3 PO Questions" above), so the PO can answer it inline rather than re-deriving what's missing. If the answer would be "TBD" or "unsure," call it an **Open Item** and flag it as a blocker before test-case writing begins — do not let an unresolved answer pass silently as a low-severity note.

## Interview Workflow

When these findings feed into an actual PO interview (e.g. via `.claude/agents/brd-clarifier.md`), ask from the Top 3 PO Questions list — only ask about gaps that are genuinely unresolved, skip what the requirement already answers.

### Step 1: Resolve the top 3 questions

Ask up to 3 questions, drawn from genuinely unresolved gaps in priority order (Top 3 PO Questions above), regardless of which dimension each belongs to.

### Step 2: Return clarified requirements block

Return the answers as:

```
## Clarified Requirements

| Ambiguity | Clarification |
|-----------|----------------|
| [ambiguity]  | [PO's answer] |
| ...          | ...           |

### Open Items

- [Any question the PO answered "unsure" or "TBD" — flag for follow-up]
```

Fill every row from the PO's answers. Move "TBD"/skipped answers to Open Items instead of a table row. Omit the Open Items section only if every ambiguity was resolved. Treat every Open Item as a blocker before test-case writing begins.

## Verification

Before marking a requirement ready for test-case writing:

- [ ] Every dimension (behaviour, constraints, conflicts) has zero unresolved findings
- [ ] Every mode/branch of a control has its own ACs
- [ ] Corner-case numbering has no gaps
- [ ] All copy strings are quoted verbatim, including interpolated variables
- [ ] Any future-state behavior is explicitly gated out of current scope
