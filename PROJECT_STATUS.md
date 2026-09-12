# Buy or Wait? — Project Status

Use this checkout as the primary review source:
`C:\hackerrank-orchestrate\hackerrank-orchestrate-september26`.

## Review policy

- Review the local checkout directly. Consult GitHub only when asked to verify
  a pushed commit or when the local checkout needs synchronizing.
- Static review only: do not run project code, tests, or dependencies on
  gitignored artifacts unless the user explicitly authorizes execution.
- Do not advance a phase from an implementation report alone; inspect the code.

## Contract reminders

The solution must write root `output.csv` with the required eight columns for
all 250 requests. It must preserve the 90-day minimum-balance rule, reserve
pending debits, exclude non-cash/unsettled credits, use confirmed salary only
on its settlement date, respect user payment preferences and protected
categories, and never invent financial facts.

## Phase tracker

- [x] Phase 1 — Deterministic data layer (`679c422`).
- [x] Phase 2 — Narrow evidence extraction (`679c422`); cached artifacts are
  gitignored and are not relied upon for static review.
- [x] Phase 3 — Reconciliation (`0b66ead`), statically approved: distinct
  active/no-change counters and non-collapsing salary amendments.
- [x] Phase 4 — Simulation and search (`ebdef74`), statically approved:
  Decimal timeline, safety predicate, safe-amount search, earliest-date search,
  fixed-recurring forecasts, and protected variable-essential forecasts.
- [x] Phase 5 — Deterministic decision and plan construction (`code/lib/decision.py`, `code/tests/test_decision.py`); candidate construction, spending changes, deterministic ranking, ready for static review.
- [ ] Phase 6 — Output verification.
- [ ] Phase 7 — Full run, sample scoring, and usage report.
- [ ] Phase 8 — Package and submit.

## Phase 5 scope

Build a separate decision layer that uses Phase 4 results and simulations to
generate eligible full-payment, partial-payment, installment, wait, and where
permitted spending-change candidates; reject unsafe candidates; and apply the
specified tie-break order. Do not generate final CSV output yet.

## Phase 5 dispatch — 2026-09-12

The implementation prompt limits this phase to a deterministic decision module
and tests. It must preserve `amount_safe_to_pay` as the no-spending-change
baseline, build only eligible and complete-by-deadline candidates, validate all
candidates through the Phase 4 simulator, and then rank them according to the
challenge tie-break order. Final output writing and validation remain Phase 6.
