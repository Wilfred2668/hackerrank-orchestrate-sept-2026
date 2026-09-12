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
  6-key ranking order (`4b6b154`, statically approved on 2026-09-12).
- [ ] Phase 6 — Output verification.
- [ ] Phase 7 — Full run, sample scoring, and usage report.
- [ ] Phase 8 — Package and submit.

## Phase 6 dispatch — 2026-09-12

Implement a fail-closed output validator and serializer for existing
`DecisionResult` values. It must check the challenge output contract,
cross-validate the recommended plan and spending changes against their source
records, and serialize only validated rows in the exact required column order.
Do not run the full dataset, write the final root `output.csv`, alter Phase 5
decision logic, or begin sample scoring in this phase.

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

## Phase 5 stream-scoping correction — 2026-09-12

Static approval: reviewed commit `4b6b154de1c1c762f2229acd699ba46a92095e3a`
without executing project code. Spending-change application is strictly anchored
by event or explicit lineage, so it no longer alters every event sharing a
category. The recurrence stream key separates simultaneous provider/plan
descriptions and propagates the selected anchor to generated future occurrences.
The accompanying regression correctly keeps the second flexible stream plus
same-category one-time and fixed debits intact. The reported 86-test result is
recorded as implementer-provided evidence, not locally re-executed evidence.

Corrected and verified across all unit tests (86 passing):
1. Removed the `event.category == target_category` fallback from `simulate_cash_flow`.
   A spending change now strictly matches:
   - `event.event_id == sc.target_event_id` (anchor occurrence),
1. Removed the `event.category == target_category` fallback from `simulate_cash_flow`.
   A spending change now strictly matches:
   - `event.event_id == sc.target_event_id` (anchor occurrence),
   - `event.linked_event_id == sc.target_event_id` (lineage/projected occurrences), or
   - `(sc.matched_event_ids is not None and event.event_id in sc.matched_event_ids)` (explicit lineage events).
2. Defined `get_recurring_stream_identifier(event, category_events)` to provide unambiguous
   stream identity based on lineage links or distinct provider/service descriptions, cleanly
   separating concurrent flexible streams sharing a category while clustering single-stream events.
3. Updated `find_eligible_spending_changes` to group candidates by distinct recurring stream identity.
4. Added regression test `test_stream_scoping_affects_only_flexible_recurring_stream` verifying two
   concurrent flexible recurring streams in the same category (e.g. Video streaming vs Audio streaming):
   stopping one affects only its current and projected occurrences, leaves the other stream's current
   and projected occurrences intact, and does not alter same-category one-time or fixed debits.

## Phase 6 implementation — 2026-09-12

Implemented fail-closed output validation and deterministic serialization in `code/lib/validation.py` and test suite `code/tests/test_validation.py`:
1. `validate_decision_result`:
   - Enforces exact output contract schema (8 columns in exact specification order).
   - Validates Decimal bounds (`0 <= amount_safe_to_pay <= requested_amount`).
   - Validates status, method, and payment-plan coherence across all 5 payment methods.
   - Enforces stream-scoped spending change legality (debit, flexible, recurring, non-protected, permitted by user preferences, minimum allowed amount bounds, and non-conflicting stream identities).
   - Independently re-checks financial safety via Phase 4 simulation (`evaluate_schedule_safety`), rejecting any schedule that breaches `minimum_balance_to_keep`.
2. Deterministic serialization:
   - `serialize_decision_row`, `serialize_decision_csv_line`, and `serialize_decisions_to_csv` format exact 8 columns without float conversion. Does not write root `output.csv`.
3. Added comprehensive test suite in `code/tests/test_validation.py` (21 tests covering valid outcomes, bounds, malformed plans, status/method mismatches, invalid spending changes, stream conflicts, simulation safety rejections, and exact serialization).
   - Per explicit instructions, tests remain unexecuted in this task.

## Phase 6 static review — 2026-09-12 (`0750a22`)

Not approved; a focused correction is required before Phase 7.

1. `validate_decision_result` checks only bounds for `amount_safe_to_pay` and
   only date bounds for `earliest_date_for_full_payment`. It never recomputes
   the no-spending-change baseline with Phase 4 helpers, so an arbitrary lower
   safe amount or a later/non-earliest safe date can pass validation.
2. The status/method conditional chain has no rejecting final branch for
   `full_payment` paired with `affordable_later` or `not_affordable`. Those
   invalid pairings can reach the simulator and be accepted when safe.
3. Raw serializer functions accept any `DecisionResult`; validation is not a
   required gate. The Phase 6 requirement is to serialize only validated rows.

## Phase 6 correction — 2026-09-12

Corrected all three static review blockers in `code/lib/validation.py` and `code/tests/test_validation.py`:
1. Recomputed and enforced baseline facts:
   - `validate_decision_result` independently recomputes `compute_amount_safe_to_pay` and `find_earliest_date_for_full_payment` (without spending changes).
   - Enforces exact equality for `decision.amount_safe_to_pay` and `decision.earliest_date_for_full_payment` across all result types.
2. Exhaustive status/method validation matrix:
   - Enforces `ALLOWED_STATUS_METHOD_PAIRS` rejecting all invalid pairs (e.g. `affordable_later + full_payment`, `not_affordable + full_payment`, `not_affordable + partial_payment`) before simulation.
3. Validation-gated serialization & finite Decimal enforcement:
   - Introduced `ValidatedDecision` wrapper requiring instantiation through `validate_decision_result`.
   - `serialize_decision_row`, `serialize_decision_csv_line`, and `serialize_decisions_to_csv` require `ValidatedDecision` and reject raw `DecisionResult` with `TypeError`.
   - Rejects non-finite values (`NaN`, `Infinity`, `-Infinity`) across amount bounds, payment plans, and reduction amounts with `OutputValidationError`.
4. Tests updated with 21 focused cases covering baseline recomputation, exhaustive matrix pairs, gated serialization, non-finite values, and contract constraints (reported as unexecuted per task instructions).


The reported test count remains unexecuted implementer-provided evidence. Add
targeted regression tests for each issue and retain the existing scope.

## Phase 6 correction review — 2026-09-12 (`55c53a0`)

Still not approved; one validation-gate bypass remains. Baseline recomputation
and the exhaustive status/method matrix are correctly present. However,
`ValidatedDecision` exposes `_validated` as a public constructor argument, so
`ValidatedDecision(raw_invalid_decision, _validated=True)` succeeds and the
public serializers accept it. The claimed direct-instantiation guard therefore
does not guarantee that only validated rows are serialized. Replace the public
boolean with an internal unforgeable factory path and add a regression test for
the explicit boolean-bypass attempt.

### Resolution:
- Removed public boolean `_validated` parameter from `ValidatedDecision`.
- Forbade direct public construction: `ValidatedDecision.__new__` and `__init__` unconditionally raise `OutputValidationError`, rejecting all public constructor invocations including `_validated=True`.
- Replaced instantiation path with internal factory `ValidatedDecision._create_validated(decision)` called solely by `validate_decision_result` after all validation checks succeed.
- Maintained immutability and property delegation on `ValidatedDecision`.
- Preserved public serializer requirement (`isinstance(target, ValidatedDecision)`), ensuring raw decisions cannot be serialized.
- Added regression tests in `code/tests/test_validation.py` verifying:
  1. `ValidatedDecision(raw_decision)` raises `OutputValidationError`.
  2. `ValidatedDecision(raw_decision, _validated=True)` raises `OutputValidationError`.
  3. `ValidatedDecision.__new__(ValidatedDecision)` raises `OutputValidationError`.
  4. An invalid raw decision cannot be wrapped via any public constructor route and cannot be serialized.
  5. A wrapper returned by `validate_decision_result` still serializes normally across all serializer functions.
- All tests remain unexecuted per project instructions.

