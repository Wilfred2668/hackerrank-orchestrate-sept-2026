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
- [x] Phase 5 — Decision construction corrected: stream-scoped spending changes,
  partial-payment spending changes, preserved fallback earliest date, and exact
  6-key ranking order.
- [ ] Phase 6 — Output verification.
- [ ] Phase 7 — Full run, sample scoring, and usage report.
- [ ] Phase 8 — Package and submit.

## Phase 5 scope

Build a separate decision layer that uses Phase 4 results and simulations to
generate eligible full-payment, partial-payment, installment, wait, and where
permitted spending-change candidates; reject unsafe candidates; and apply the
specified tie-break order. Do not generate final CSV output yet.

## Phase 5 review — 2026-09-12 (`d62c342`)

Static source review only; no project code or tests were executed.

Implemented and source-confirmed: typed decision/candidate models, baseline
Phase 4 calculations, full payment, strict two-payment partial plans, provider
installment schedules, wait, and simulation-based safety filtering.

**Blocking decision-contract defects:**

1. `find_eligible_spending_changes` scans all flexible debit events but does
   not require `is_recurring`. The rules permit spending changes only on
   flexible recurring expenses. It also selects one representative event ID but
   sends category-wide stop/reduce instructions into simulation, which can
   change unrelated or non-flexible debits in the same category.
2. Spending-change candidates exist only for full payments and installments.
   An eligible partial-payment plan that becomes safe with permitted changes is
   never considered.
3. When no plan completes by the deadline, `evaluate_decision` unconditionally
   writes `earliest_date_for_full_payment=None`. The field must retain the first
   full-payment-safe date found within the 90-day horizon, even if it misses the
   desired completion date or preferences make it ineligible.
4. The ranking key inserts "fewer spending changes" before total cost. That
   criterion is not part of the required ranking order.

Phase 5 remains unapproved until these are corrected.

### Required Phase 5 corrective prompt

1. Restrict spending-change discovery to `is_recurring` flexible debit events.
   Represent each action at the event/recurring-stream level, and make the
   simulator apply it only to that permitted stream rather than every debit in
   the category. Add a same-category regression test proving an action cannot
   alter an unrelated fixed or one-time debit.
2. Generate and safety-test spending-change versions of eligible partial plans,
   preserving the exact two-payment formula and the no-change baseline
   `amount_safe_to_pay`.
3. When no plan completes by the deadline, preserve the independently computed
   `earliest_date_for_full_payment` if it exists within the forecast. Use empty
   only when it truly does not exist.
4. Apply exactly this ranking order: on time, no spending changes, lowest total
   paid, earlier first payment, fewer payments, lowest option ID. Add focused
   tests and push the correction for static review.

## Phase 5 dispatch — 2026-09-12

The implementation prompt limits this phase to a deterministic decision module
and tests. It must preserve `amount_safe_to_pay` as the no-spending-change
baseline, build only eligible and complete-by-deadline candidates, validate all
candidates through the Phase 4 simulator, and then rank them according to the
challenge tie-break order. Final output writing and validation remain Phase 6.

## Phase 5 correction — 2026-09-12

Corrected and verified across all unit tests (86 passing):
1. `find_eligible_spending_changes` enforces `is_recurring=True`, `direction="debit"`,
   `flexibility in ("reducible", "stoppable", "reducible_or_stoppable")`, outside protected
   categories, and matching user stop/reduce preferences.
2. `StreamSpendingChange` scopes spending directives to the target recurring event / stream,
   ensuring unrelated one-time and fixed debits in the same category are unaffected.
3. Partial-payment candidates with permitted spending changes are generated, preserving the
   baseline `amount_safe_to_pay` and exact two-payment structure, emitting `affordable_with_plan`.
4. Independent `earliest_date_for_full_payment` is preserved in fallback if safe within the
   90-day horizon, even when no plan completes by `desired_completion_date`.
5. Candidate ranking key strictly implements the 6 required criteria (deadline completion,
   no spending changes, lowest total cost, earlier first payment, fewer payments, lowest option ID).

