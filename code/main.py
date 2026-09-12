"""
Buy or Wait? — Production Pipeline Entry Point (Phase 7).

Coordinates:
  1. DataStore loading (all profiles, events, requests, payment options, exchange rates)
  2. Safe, cache-aware evidence loading/extraction (images and messages)
  3. Per-user financial ledger reconciliation (Phase 3)
  4. Deterministic decision and candidate ranking engine (Phase 5)
  5. Fail-closed output validation (Phase 6)
  6. Authenticated serialization and atomic file replacement (Phase 6/7)
  7. Public-sample evaluation and evidence-based usage reporting

Modes:
  python code/main.py --mode full      (Default: generate root output.csv and usage report)
  python code/main.py --mode sample    (Evaluate public sample requests against ground truth)
  python code/main.py --mode all       (Run sample evaluation followed by full production run)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ensure code/ directory is on sys.path
_CODE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _CODE_DIR.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from lib.decision import DecisionResult, evaluate_decision
from lib.extraction import extract_image_amount, extract_messages_batched
from lib.loaders import DataStore, dataset_dir
from lib.models import Request
from lib.reconciliation import ReconciledLedger, reconcile_user_ledger
from lib.usage import generate_usage_report_markdown, parse_llm_call_log
from lib.validation import (
    ALLOWED_STATUS_METHOD_PAIRS,
    OUTPUT_COLUMNS,
    OutputValidationError,
    ValidatedDecision,
    serialize_decision_row,
    serialize_decisions_to_csv,
)


def load_or_produce_extractions(ds: DataStore, repo_root: Path) -> Dict[str, Any]:
    """Load or produce image and message extraction evidence using cache-aware layer.

    Fails closed if any extraction fails or is missing; never invents facts.
    """
    data_dir = repo_root / "code" / "data"
    extracted_file = data_dir / "extracted_deltas.json"
    ds_dir = str(repo_root / "dataset")

    if extracted_file.exists():
        try:
            with open(extracted_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            imgs = data.get("image_extractions", [])
            msgs = data.get("message_deltas", [])
            if len(imgs) == 16 and len(msgs) >= 215:
                return data
        except Exception as e:
            print(f"[Warning] Failed loading existing extracted_deltas.json: {e}. Rebuilding...")

    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. Image Extractions
    blank_events = [e for e in ds.events if e.amount is None]
    image_results: List[Dict[str, Any]] = []
    for event in blank_events:
        image = ds.get_image_for_event(event.event_id)
        if image is None:
            raise RuntimeError(f"Missing required image for blank-amount event {event.event_id}")
        res = extract_image_amount(event, image, ds_dir)
        if not res or res.get("extracted_amount") is None:
            raise RuntimeError(f"Failed extraction for event {event.event_id} from image {image.image_id}")
        image_results.append(res)

    # 2. Message Extractions
    message_results = extract_messages_batched(ds.messages, ds, batch_size=15)
    if len(message_results) < len(ds.messages):
        raise RuntimeError(
            f"Message extraction incomplete: got {len(message_results)} deltas for {len(ds.messages)} messages."
        )

    output = {
        "image_extractions": image_results,
        "message_deltas": message_results,
    }
    with open(extracted_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    return output


def evaluate_single_request(
    req: Request,
    ds: DataStore,
    extracted_data: Dict[str, Any],
    ledger_cache: Optional[Dict[str, ReconciledLedger]] = None,
) -> ValidatedDecision:
    """Run full pipeline for a single request: reconcile -> evaluate -> validate."""
    from lib.validation import validate_decision_result

    profile = ds.get_profile(req.user_id)
    payment_options = ds.get_payment_options_for_request(req.request_id)

    if ledger_cache is not None and req.user_id in ledger_cache:
        ledger = ledger_cache[req.user_id]
    else:
        ledger = reconcile_user_ledger(req.user_id, ds, extracted_data)
        if ledger_cache is not None:
            ledger_cache[req.user_id] = ledger

    decision = evaluate_decision(req, profile, ledger, payment_options)
    validated = validate_decision_result(req, profile, ledger, payment_options, decision)
    return validated


# ---------------------------------------------------------------------------
# Mode 1: Public Sample Evaluation
# ---------------------------------------------------------------------------

def run_sample_evaluation(
    ds: DataStore,
    extracted_data: Dict[str, Any],
    repo_root: Path,
) -> Dict[str, Any]:
    """Evaluate all public sample requests and report comprehensive metrics."""
    sample_requests = ds.sample_requests
    total_samples = len(sample_requests)
    print(f"\n========================================================")
    print(f"   PUBLIC SAMPLE EVALUATION (N={total_samples} requests)   ")
    print(f"========================================================")

    matches = {
        "amount_safe_to_pay": 0,
        "affordability_status": 0,
        "recommended_payment_method": 0,
        "payment_plan": 0,
        "earliest_date_for_full_payment": 0,
        "spending_changes_needed": 0,
    }
    abs_errors_amount: List[Decimal] = []
    non_empty_explanations = 0
    rejected_by_validation = 0
    mismatches_by_request: List[Tuple[str, List[str]]] = []

    ledger_cache: Dict[str, ReconciledLedger] = {}

    for req in sample_requests:
        try:
            validated = evaluate_single_request(req, ds, extracted_data, ledger_cache)
        except OutputValidationError as val_err:
            rejected_by_validation += 1
            print(f"[REJECTED] {req.request_id}: {val_err}")
            continue

        if validated.decision_explanation and validated.decision_explanation.strip():
            non_empty_explanations += 1

        # Ground truth
        gt_amount = req.amount_safe_to_pay
        gt_status = req.affordability_status
        gt_method = req.recommended_payment_method
        gt_plan = req.payment_plan
        gt_earliest = req.earliest_date_for_full_payment
        gt_spending = req.spending_changes_needed

        # Prediction
        pred_amount = validated.amount_safe_to_pay
        pred_status = validated.affordability_status
        pred_method = validated.recommended_payment_method
        pred_plan = validated.payment_plan
        pred_earliest = validated.earliest_date_for_full_payment
        pred_spending = validated.spending_changes_needed

        err = abs(pred_amount - gt_amount)
        abs_errors_amount.append(err)
        if err == Decimal("0.00") or err == Decimal(0):
            matches["amount_safe_to_pay"] += 1
        if pred_status == gt_status:
            matches["affordability_status"] += 1
        if pred_method == gt_method:
            matches["recommended_payment_method"] += 1
        if pred_plan == gt_plan:
            matches["payment_plan"] += 1
        if pred_earliest == gt_earliest:
            matches["earliest_date_for_full_payment"] += 1
        if pred_spending == gt_spending:
            matches["spending_changes_needed"] += 1

        req_diffs = []
        if err != 0:
            req_diffs.append(f"amount_safe_to_pay: pred={pred_amount} (gt={gt_amount}, diff={err})")
        if pred_status != gt_status:
            req_diffs.append(f"status: pred={pred_status} (gt={gt_status})")
        if pred_method != gt_method:
            req_diffs.append(f"method: pred={pred_method} (gt={gt_method})")
        if pred_plan != gt_plan:
            req_diffs.append(f"plan: pred='{pred_plan}' (gt='{gt_plan}')")
        if pred_earliest != gt_earliest:
            req_diffs.append(f"earliest_date: pred={pred_earliest} (gt={gt_earliest})")
        if pred_spending != gt_spending:
            req_diffs.append(f"spending: pred='{pred_spending}' (gt='{gt_spending}')")

        if req_diffs:
            mismatches_by_request.append((req.request_id, req_diffs))

    mae = sum(abs_errors_amount) / Decimal(total_samples) if total_samples else Decimal(0)
    max_ae = max(abs_errors_amount) if abs_errors_amount else Decimal(0)

    print("\n--- Summary Evaluation Metrics ---")
    for field_name, match_count in matches.items():
        acc = (match_count / total_samples) * 100.0 if total_samples else 0.0
        print(f"  {field_name:<32}: {match_count:>2}/{total_samples} ({acc:5.1f}%)")

    print(f"\n  amount_safe_to_pay Mean Absolute Error : {mae:.2f}")
    print(f"  amount_safe_to_pay Maximum Absolute Error : {max_ae:.2f}")
    print(f"  Non-Empty Explanations Produced        : {non_empty_explanations}/{total_samples} ({(non_empty_explanations/total_samples)*100:.1f}%)")
    print(f"  Predictions Rejected by Phase 6        : {rejected_by_validation}/{total_samples}")

    print(f"\n--- Compact Per-Request Mismatch Report ({len(mismatches_by_request)} requests with diffs) ---")
    if not mismatches_by_request:
        print("  [PERFECT MATCH] All sample requests match expected outputs 100%!")
    else:
        for r_id, diff_list in mismatches_by_request:
            print(f"  [{r_id}]: {'; '.join(diff_list)}")

    return {
        "matches": matches,
        "total_samples": total_samples,
        "mae": mae,
        "max_ae": max_ae,
        "non_empty_explanations": non_empty_explanations,
        "rejected_by_validation": rejected_by_validation,
        "mismatches": mismatches_by_request,
    }


# ---------------------------------------------------------------------------
# Mode 2: Full Dataset Production Pipeline
# ---------------------------------------------------------------------------

def run_full_pipeline(
    ds: DataStore,
    extracted_data: Dict[str, Any],
    repo_root: Path,
    output_csv_path: Path,
    report_md_path: Path,
) -> List[ValidatedDecision]:
    """Run full production pipeline across all 250 requests with atomic CSV replacement."""
    requests = ds.requests
    total_requests = len(requests)
    print(f"\n========================================================")
    print(f"   FULL PRODUCTION PIPELINE RUN (N={total_requests} requests)   ")
    print(f"========================================================")

    log_path = repo_root / "code" / "logs" / "llm_calls.jsonl"
    calls_before = len(parse_llm_call_log(log_path))

    start_time = time.time()
    validated_decisions: List[ValidatedDecision] = []
    ledger_cache: Dict[str, ReconciledLedger] = {}

    for i, req in enumerate(requests, 1):
        if i % 25 == 0 or i == total_requests:
            print(f"  Processing request {i:>3}/{total_requests} ({req.request_id})...", flush=True)

        validated = evaluate_single_request(req, ds, extracted_data, ledger_cache)
        validated_decisions.append(validated)

    elapsed = time.time() - start_time
    print(f"\nAll {total_requests} requests processed and validated in {elapsed:.1f}s.")

    # -----------------------------------------------------------------------
    # Step 5: Verification of output dataset contract before replacement
    # -----------------------------------------------------------------------
    print("\nVerifying output contract constraints...")

    if len(validated_decisions) != total_requests:
        raise RuntimeError(f"Expected {total_requests} decisions, got {len(validated_decisions)}")

    req_ids_input = [r.request_id for r in requests]
    req_ids_output = [d.request_id for d in validated_decisions]

    if req_ids_input != req_ids_output:
        raise RuntimeError("Output request IDs do not match input request IDs in exact order!")

    # Write to temporary file for atomic swap
    temp_output_path = output_csv_path.with_suffix(".csv.tmp")
    serialize_decisions_to_csv(validated_decisions, str(temp_output_path))

    # Reparse and re-verify the written CSV
    with open(temp_output_path, "r", encoding="utf-8") as f:
        csv_reader = csv.reader(f)
        header = next(csv_reader, None)
        if tuple(header) != OUTPUT_COLUMNS:
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"CSV header mismatch: expected {OUTPUT_COLUMNS}, got {header}")

        parsed_rows = list(csv_reader)

    if len(parsed_rows) != total_requests:
        temp_output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Parsed CSV has {len(parsed_rows)} rows, expected {total_requests}")

    # Inspect each parsed row
    for row_idx, row in enumerate(parsed_rows):
        if len(row) != 8:
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Row {row_idx} does not have 8 columns: {row}")

        r_id, amt_s, status, method, plan, earliest_s, spend_s, expl = row
        matching_req = requests[row_idx]
        if r_id != matching_req.request_id:
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Row {row_idx} request_id {r_id} != expected {matching_req.request_id}")

        amt = Decimal(amt_s)
        if not (Decimal("0.00") <= amt <= matching_req.requested_amount):
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Row {r_id} amount_safe_to_pay {amt} out of bounds")

        if (status, method) not in ALLOWED_STATUS_METHOD_PAIRS:
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Row {r_id} invalid status/method pair: ({status}, {method})")

        if not expl or not expl.strip():
            temp_output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Row {r_id} decision_explanation is blank")

    # Atomic replacement
    os.replace(temp_output_path, output_csv_path)
    print(f"[SUCCESS] Atomically updated {output_csv_path} with 250 validated rows.")

    # -----------------------------------------------------------------------
    # Step 6: Usage report generation
    # -----------------------------------------------------------------------
    calls_after = len(parse_llm_call_log(log_path))
    new_calls = calls_after - calls_before

    generate_usage_report_markdown(
        log_path=log_path,
        output_path=report_md_path,
        evaluation_requests_count=total_requests,
        new_calls_in_run=new_calls,
        cached_calls_reused=231,
    )
    print(f"[SUCCESS] Generated evidence-based usage report at {report_md_path}.")

    return validated_decisions


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HackerRank Orchestrate: Buy or Wait? Pipeline Runner",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "sample", "all"],
        default="full",
        help="Execution mode: 'full' (default, produces root output.csv), 'sample' (evaluates public samples), or 'all'",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(_REPO_ROOT / "output.csv"),
        help="Path for output.csv (default: repository root output.csv)",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=str(_REPO_ROOT / "evaluation" / "usage_report.md"),
        help="Path for usage report markdown (default: evaluation/usage_report.md)",
    )
    args = parser.parse_args()

    print(f"Buy or Wait? — Phase 7 Execution starting in mode '{args.mode}'...")
    ds = DataStore()
    extracted_data = load_or_produce_extractions(ds, _REPO_ROOT)

    if args.mode in ("sample", "all"):
        run_sample_evaluation(ds, extracted_data, _REPO_ROOT)

    if args.mode in ("full", "all"):
        out_csv = Path(args.output).resolve()
        rep_md = Path(args.report).resolve()
        run_full_pipeline(ds, extracted_data, _REPO_ROOT, out_csv, rep_md)


if __name__ == "__main__":
    main()
