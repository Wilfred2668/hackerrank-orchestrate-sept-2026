"""
Phase 2 runner — extract structured facts from all 16 images and 215 messages.

Writes results to code/data/extracted_deltas.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure code/ is on sys.path
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__))))

from lib.loaders import DataStore, dataset_dir
from lib.extraction import extract_image_amount, extract_message_delta, extract_messages_batched

_DATA_DIR = Path(__file__).resolve().parent / "data"
_OUTPUT_FILE = _DATA_DIR / "extracted_deltas.json"


def run_image_extractions(ds: DataStore, ds_dir: str) -> list:
    """Extract amounts from all 16 images linked to blank-amount events."""
    blank_events = [e for e in ds.events if e.amount is None]
    print(f"[Phase 2A] Extracting amounts from {len(blank_events)} images...")

    results = []
    for i, event in enumerate(blank_events, 1):
        image = ds.get_image_for_event(event.event_id)
        if image is None:
            print(f"  WARNING: No image for {event.event_id}")
            continue

        print(f"  [{i}/{len(blank_events)}] {event.event_id} ({image.image_id})...", end=" ", flush=True)
        try:
            result = extract_image_amount(event, image, ds_dir)
            print(f"-> {result['extracted_amount']} ({result['confidence']})")
            results.append(result)
        except Exception as ex:
            print(f"ERROR: {ex}")
            results.append({
                "event_id": event.event_id,
                "extracted_amount": "0",
                "currency": event.currency,
                "confidence": "low",
                "reasoning": f"Extraction failed: {ex}",
                "source_image_id": image.image_id,
            })

    return results


def run_message_extractions(ds: DataStore) -> list:
    """Classify all 215 messages in batches of ~15."""
    print(f"\n[Phase 2B] Classifying {len(ds.messages)} messages in batches...", flush=True)
    return extract_messages_batched(ds.messages, ds, batch_size=15)


def main() -> None:
    start = time.time()
    ds = DataStore()
    ds_dir = dataset_dir()

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Part A: Images
    image_results = run_image_extractions(ds, ds_dir)

    # Part B: Messages
    message_results = run_message_extractions(ds)

    # Write combined output
    output = {
        "image_extractions": image_results,
        "message_deltas": message_results,
    }
    with open(_OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    elapsed = time.time() - start
    print(f"\n[Phase 2] Done in {elapsed:.1f}s", flush=True)
    print(f"  Image extractions: {len(image_results)}", flush=True)
    print(f"  Message deltas: {len(message_results)}", flush=True)

    # Summary of message actions
    from collections import Counter
    actions = Counter(r.get("action", "?") for r in message_results)
    print(f"  Message action breakdown: {dict(actions)}", flush=True)

    print(f"\nResults written to {_OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
