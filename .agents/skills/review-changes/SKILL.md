---
name: review-changes
description: Review a change set through six independent review areas and consolidate actionable findings before fixes. Use when the user asks for a broad code review or invokes this skill; ordinary implementation and focused debugging do not require it.
---

# Review changes

Review the requested changes without editing them. Produce a discussion-ready
assessment of real problems, useful simplifications, and remaining uncertainty.
Repository-specific standards come from applicable AGENTS.md files.

## Establish scope

Identify the requested diff, commit range, or checkpoint. Include relevant
untracked source/tests. If the tree is clean, use the user-specified committed
change; clarify the base only when it cannot reasonably be inferred.

Inspect nearby callers and tests to understand behavior, but keep findings focused
on the change. Distinguish existing defects, accepted tradeoffs, and features
explicitly deferred to later tickets. Read scratch plans only when available and
relevant; the skill must work without private scratch files.

## Independent reviewers

Assign one subagent per area:

1. Security: injection, secret handling, access boundaries, and unsafe file/network operations.
2. Code quality: clarity, naming, style consistency, nesting, and unnecessary complexity.
3. Bugs and logic: realistic behavior failures and regressions against the intended contract.
4. Concurrency: races, lifecycle, locking, interruption, and resource ownership.
5. Tests: missing behavioral coverage, unreliable fixtures, timeouts, and platform differences.
6. Maintainability: responsibility boundaries, implicit interactions, and future change costs.

Give each reviewer the same scope and raw code/tests, plus their specific focus.
Ask for severity, file/line references, realistic trigger and impact, and evidence
or an isolated reproduction where practical. Do not seed reviewers with the answer
or other reviewers' conclusions before their independent assessment.

Reviewers must leave source and Git state unchanged. Probes may use temporary
fixtures; they must not access real secrets, upload books, mutate live libraries,
or depend on external side effects. Reuse relevant test results when sufficient.

Respect available agent slots; run batches if necessary. Wait for all six reviewers
before presenting the consolidated result. If delegation is unavailable, explain
that limitation and label any single-agent review accurately.

## Consolidate

Check findings against the code and intended behavior. Deduplicate overlapping
findings, reconcile conflicting claims, and separate demonstrated defects from
conditional hardening or stylistic preferences. Do not invent findings for an area
that has none, and do not treat a passing suite as proof of correctness.

Summarize each of the six areas, including an explicit no-findings result when
appropriate. Lead with actionable higher-severity issues; include precise local
file/line links and relevant test evidence. State limitations and untested platforms.
Offer a practical fix order, keeping small independent changes reviewable.

Do not start fixes, create external issues/comments, or commit changes as part of
the review unless the user has also requested those actions. Save a report only
when requested or when the repository's scratch convention permits it. For a
follow-up review, recheck prior findings and distinguish resolved, deferred, and
new issues without simply repeating the earlier verdict.
