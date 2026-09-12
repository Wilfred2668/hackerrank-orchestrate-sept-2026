"""
Evidence extraction layer — turns images and messages into structured,
confidence-scored JSON for Phase 3 (reconciliation).

This module extracts facts AS CLAIMED by the source; it never decides
which conflicting fact wins — that is Phase 3's job.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from . import llm_client
from .loaders import DataStore, dataset_dir
from .models import FinancialEvent, Image, Message


# ---------------------------------------------------------------------------
# Part A: Image Amount Extraction
# ---------------------------------------------------------------------------

_IMAGE_PROMPT_TEMPLATE = """You are a financial document reader. Extract the correct monetary amount from this image.

CONTEXT about the financial event this image is associated with:
- Event ID: {event_id}
- Description: {description}
- Category: {category}
- Currency: {currency}
- Direction: {direction}

EXTRACTION RULES (apply in order):
1. NET vs GROSS: If this is a payslip and the event description mentions "net" (e.g. "net salary", "net pay"), extract the Net Pay figure, NOT gross earnings or any allowance line item.
2. MULTIPLE TOTALS: When multiple total-type fields are shown (e.g. "Total Amount", "Amount Received", "Balance Due"), extract the field whose LABEL semantically matches the event description. For example:
   - If description says "outstanding balance" → extract "Balance Due", not "Total" or "Amount Received"
   - If description says "payment" or "amount due" → extract the amount actually owed
3. ROUNDING: If a document shows both a precise computed total (with fractional amounts from tax math) AND a separate "Grand Total" that is rounded and marked as the actually charged/paid amount, prefer the rounded actually-charged figure.
4. TENDERED vs OWED: If a receipt shows "Cash Paid"/"Amount Tendered" alongside a "Total" and "Change" line, the amount owed is the Total, NEVER the cash tendered.
5. CONDITIONAL/PENALTY: If a bill shows a base amount due by a certain date and a higher amount after a late-payment date, extract the base billed amount unless there's evidence the late fee applies.
6. TRUNCATED/INCOMPLETE: If the image appears cut off, extract the last fully visible total-type figure, and note what was cut off.
7. HANDWRITTEN: Cross-validate extracted total against the sum of visible line items when legibility is uncertain. If they disagree, trust the line-item sum.
8. AMOUNT IN WORDS: If an "amount in words" field is present, the numeral that agrees with the written-out amount is the correct one.

IMPORTANT: 
- Use the Indian numbering system (lakhs/crores with commas like 1,00,000) when the currency is INR. Convert to plain number (e.g. 100000).
- Use standard international notation for other currencies.
- Return ONLY the numeric amount without currency symbols, commas, or units.

Respond with ONLY a valid JSON object (no markdown, no code fences):
{{
  "extracted_amount": "<number as string>",
  "confidence": "high" or "medium" or "low",
  "reasoning": "<brief explanation of which field was extracted and why>"
}}
"""


def extract_image_amount(
    event: FinancialEvent,
    image: Image,
    ds_dir: str,
) -> Dict[str, Any]:
    """Extract the monetary amount from an image for a blank-amount event."""
    image_path = image.resolve_path(ds_dir)

    prompt = _IMAGE_PROMPT_TEMPLATE.format(
        event_id=event.event_id,
        description=event.description,
        category=event.category,
        currency=event.currency,
        direction=event.direction,
    )

    raw = llm_client.call_vision(
        prompt,
        image_path,
        call_type="image_extraction",
        target_id=event.event_id,
    )

    # Parse JSON from response
    parsed = _parse_json_response(raw)

    return {
        "event_id": event.event_id,
        "extracted_amount": parsed.get("extracted_amount", "0"),
        "currency": event.currency,
        "confidence": parsed.get("confidence", "low"),
        "reasoning": parsed.get("reasoning", ""),
        "source_image_id": image.image_id,
    }


# ---------------------------------------------------------------------------
# Part B: Message Delta Extraction
# ---------------------------------------------------------------------------

_MESSAGE_PROMPT_TEMPLATE = """You are a financial message analyst. Classify this message and extract any structured financial delta.

MESSAGE:
- Message ID: {message_id}
- User ID: {user_id}
- Request ID: {request_id}
- Related Event ID: {related_event_id}
- Sent at: {sent_at}
- Source type: {source_type}
- Message text: {message_text}

RELATED EVENT CONTEXT (if related_event_id is populated):
{event_context}

USER'S RECENT FINANCIAL EVENTS IN RELEVANT CATEGORY:
{recent_events_context}

CLASSIFICATION RULES:
1. "confirm_no_change" — The message is purely informational; the financial_events data already correctly reflects the fact described. Examples:
   - Confirmation that a pending payment is still pending
   - Confirmation that a refund hasn't landed yet
   - Notification that an unrealized investment value hasn't changed
   - A general status update with no new financial data
   - A bonus/commission/credit still under review or waiting for approval (not yet confirmed)
   DO NOT invent a ledger change just because a message exists. Most messages are confirmatory.

2. "amend" — The message changes an amount, date, or status of a specific financial fact. Examples:
   - A new salary amount taking effect from a specific date
   - A rent increase (percentage or absolute)
   - A revised invoice amount
   - A temporary pay change (reduced/increased salary for specific period)

3. "cancel" — An event or scheduled fact is explicitly cancelled or reversed. Example:
   - "Your subscription has been cancelled effective immediately"

4. "delay" — A date is pushed later. Example:
   - "Your delivery and charge will be delayed to [new date]"

5. "new_fact" — Introduces a new confirmed financial fact not otherwise present. Examples:
   - A newly approved invoice payment with amount and settlement date
   - Prize/lottery winnings that have been CONFIRMED SETTLED ("reached your account", "claim closed")
   Note: Prize/lottery amounts still "pending"/"processing" are NOT new_fact — they are confirm_no_change since the base data already shows them as pending.

SPECIAL: RELATIVE CHANGES
If the message describes a percentage increase/decrease or "increased by X", you MUST compute the absolute value:
- Find the most recent amount for that recurring category from the event context provided
- Show the computation: "previous_amount * (1 + percentage/100) = new_amount"
- Return the computed absolute new_value

LANGUAGE: The message may be in English or Bahasa Indonesia. Read and understand it directly — do NOT translate.

Respond with ONLY a valid JSON object (no markdown, no code fences):

If confirm_no_change:
{{
  "action": "confirm_no_change",
  "reasoning": "<brief explanation>"
}}

Otherwise:
{{
  "action": "<amend|cancel|delay|new_fact>",
  "target": "<event_id or 'user_general'>",
  "field": "<amount|date|status|recurring_value>",
  "new_value": "<the new value as string>",
  "effective_date": "<YYYY-MM-DD or null if not specified>",
  "computation_note": "<show calculation if derived from relative change, else null>",
  "confidence": "<high|medium|low>",
  "source_language": "<en|id>",
  "reasoning": "<brief explanation>"
}}
"""


def _get_event_context(event_id: Optional[str], ds: DataStore) -> str:
    """Get context about the related event if it exists."""
    if not event_id:
        return "No related event."
    events = [e for e in ds.events if e.event_id == event_id]
    if not events:
        return f"Event {event_id} not found in dataset."
    e = events[0]
    return (
        f"Event: {e.event_id}, type={e.event_type}, desc='{e.description}', "
        f"category={e.category}, direction={e.direction}, "
        f"amount={e.amount}, currency={e.currency}, "
        f"event_date={e.event_date}, settlement_date={e.settlement_date}, "
        f"status={e.status}, flexibility={e.flexibility}"
    )


def _get_recent_events_for_user_category(
    user_id: str,
    msg: Message,
    ds: DataStore,
) -> str:
    """Get the most recent events for the user that might be relevant to computing
    relative changes (e.g. salary, rent amounts)."""
    user_events = ds.get_events_for_user(user_id)
    if not user_events:
        return "No events found for this user."

    # If there's a related event, get events in the same category
    related_category = None
    if msg.related_event_id:
        related = [e for e in user_events if e.event_id == msg.related_event_id]
        if related:
            related_category = related[0].category

    # Get recent recurring events (salary, rent, etc.) — last 5 per relevant category
    relevant = []
    for e in user_events:
        if related_category and e.category == related_category:
            relevant.append(e)
        elif e.event_type in ("income", "expense") and e.category in (
            "salary", "rent", "utilities", "insurance", "subscription",
            "housing", "groceries",
        ):
            relevant.append(e)

    # Sort by event_date desc, take last 10
    relevant.sort(key=lambda e: e.event_date, reverse=True)
    relevant = relevant[:10]

    if not relevant:
        return "No recent recurring events found."

    lines = []
    for e in relevant:
        lines.append(
            f"  {e.event_id}: {e.event_type}/{e.category}, "
            f"amount={e.amount} {e.currency}, "
            f"date={e.event_date}, status={e.status}"
        )
    return "\n".join(lines)


def _build_message_result(msg: Message, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a parsed LLM dictionary into the standard message delta output schema."""
    action = parsed.get("action", "confirm_no_change")
    result: Dict[str, Any] = {"message_id": msg.message_id, "action": action}

    if action == "confirm_no_change":
        result["reasoning"] = parsed.get("reasoning", "")
    else:
        # Invariant 1: target may ONLY be set to a specific event_id if msg.related_event_id is populated!
        # Otherwise it MUST be "user_general". Matching general facts to events is Phase 3's job.
        if msg.related_event_id:
            target = msg.related_event_id
        else:
            target = "user_general"

        # Invariant 2: unquantifiable amounts must be None (null), never placeholder strings like "childcare payment"
        raw_new_val = parsed.get("new_value")
        if raw_new_val is None or str(raw_new_val).lower() in ("null", "none", "", "unspecified"):
            new_val = None
        else:
            raw_str = str(raw_new_val).strip()
            # Clean currency symbols and check for numbers
            cleaned = re.sub(r"\b(EUR|USD|INR|ZAR|IDR|Rp|Rs|\$)\b", "", raw_str, flags=re.IGNORECASE).strip()
            cleaned_num = cleaned.replace(",", "")
            nums = re.findall(r"\d+(?:\.\d+)?", cleaned_num)
            if nums:
                try:
                    Decimal(cleaned_num)
                    new_val = cleaned_num
                except (InvalidOperation, ValueError):
                    new_val = nums[0]
            else:
                new_val = None

        confidence = parsed.get("confidence", "low" if new_val is None else "high")
        if new_val is None:
            confidence = "low"

        result.update({
            "user_id": msg.user_id,
            "target": target,
            "field": parsed.get("field", "recurring_value"),
            "new_value": new_val,
            "effective_date": parsed.get("effective_date"),
            "computation_note": parsed.get("computation_note"),
            "confidence": confidence,
            "source_language": parsed.get("source_language", "en"),
            "reasoning": parsed.get("reasoning", ""),
        })

    return result


def extract_message_delta(
    msg: Message,
    ds: DataStore,
) -> List[Dict[str, Any]]:
    """Classify a single message and extract structured deltas (returns a list of deltas)."""
    cached = llm_client.get_cached("message_extraction", msg.message_id)
    if cached is not None:
        parsed = _parse_json_or_array_response(cached)
        if isinstance(parsed, list):
            return [_build_message_result(msg, p) for p in parsed]
        return [_build_message_result(msg, parsed)]

    event_context = _get_event_context(msg.related_event_id, ds)
    recent_context = _get_recent_events_for_user_category(msg.user_id, msg, ds)

    prompt = _MESSAGE_PROMPT_TEMPLATE.format(
        message_id=msg.message_id,
        user_id=msg.user_id,
        request_id=msg.request_id or "(none)",
        related_event_id=msg.related_event_id or "(none)",
        sent_at=msg.sent_at.isoformat(),
        source_type=msg.source_type,
        message_text=msg.message_text,
        event_context=event_context,
        recent_events_context=recent_context,
    )

    raw = llm_client.call_text(
        prompt,
        call_type="message_extraction",
        target_id=msg.message_id,
    )

    parsed = _parse_json_or_array_response(raw)
    if isinstance(parsed, list):
        items = [_build_message_result(msg, p) for p in parsed]
    else:
        items = [_build_message_result(msg, parsed)]

    llm_client.set_cache("message_extraction", msg.message_id, json.dumps(parsed))
    return items


# ---------------------------------------------------------------------------
# Batched Message Extraction
# ---------------------------------------------------------------------------

_BATCH_MESSAGE_PROMPT_TEMPLATE = """You are a financial message analyst. Classify the following batch of {count} messages and extract any structured financial deltas.

CLASSIFICATION RULES:
1. "confirm_no_change" — The message is purely informational; the financial_events data already correctly reflects the fact described. Examples:
   - Confirmation that a pending payment is still pending
   - Confirmation that a refund hasn't landed yet
   - Notification that an unrealized investment value hasn't changed
   - A general status update with no new financial data
   - A bonus/commission/credit still under review or waiting for approval (not yet confirmed)
   - Inter-account transfer between user's own accounts
   DO NOT invent a ledger change just because a message exists. Most messages are confirmatory.

2. "amend" — The message changes an amount, date, or status of a specific financial fact. Examples:
   - A new salary amount taking effect from a specific date
   - A rent increase (percentage or absolute)
   - A revised invoice amount
   - A temporary pay change (reduced/increased salary for specific period)

3. "cancel" — An event or scheduled fact is explicitly cancelled or reversed. Example:
   - "Your subscription has been cancelled effective immediately"

4. "delay" — A date is pushed later. Example:
   - "Your delivery and charge will be delayed to [new date]"

5. "new_fact" — Introduces a new confirmed financial fact not otherwise present. Examples:
   - A newly approved invoice payment with amount and settlement date
   - Prize/lottery winnings that have been CONFIRMED SETTLED ("reached your account", "claim closed")
   - A one-time arrears adjustment payment
   - A newly introduced recurring commitment
   Note: Prize/lottery amounts still "pending"/"processing" are NOT new_fact — they are confirm_no_change since the base data already shows them as pending.

TARGET INVARIANT (MANDATORY):
- If the message's Related Event ID is populated, target MUST be that event_id (e.g. "event_1234").
- If Related Event ID is (none) or blank, target MUST be "user_general".
- NEVER infer, guess, or attach a specific event_id from user event history when Related Event ID is (none).

MULTI-CLAUSE MESSAGES (MANDATORY):
When a message contains MULTIPLE distinct financial facts in separate clauses, emit MULTIPLE delta objects with the SAME message_id, one object per fact:
Example A:
"Regular salary of EUR 2717 resumes on 2025-08-15. A new recurring childcare payment begins in the same month."
-> Emit TWO objects for this message_id:
   1) {{ "message_id": "...", "action": "amend", "target": "user_general", "field": "recurring_value", "new_value": "2717", "effective_date": "2025-08-15", "confidence": "high", "reasoning": "Regular salary resumes at 2717 on 2025-08-15." }}
   2) {{ "message_id": "...", "action": "new_fact", "target": "user_general", "field": "recurring_value", "new_value": null, "effective_date": "2025-08-01", "confidence": "low", "reasoning": "A new recurring childcare payment begins in August 2025, but the amount is unspecified in the source." }}

Example B:
"Your regular salary for the next payroll is EUR 1452. The same payroll includes a one-time arrears adjustment of EUR 653.40."
-> Emit TWO objects for this message_id:
   1) {{ "message_id": "...", "action": "amend", "target": "user_general", "field": "recurring_value", "new_value": "1452", "effective_date": null, "confidence": "high", "reasoning": "Regular salary updated for next payroll." }}
   2) {{ "message_id": "...", "action": "new_fact", "target": "user_general", "field": "amount", "new_value": "653.40", "effective_date": null, "confidence": "high", "reasoning": "One-time arrears adjustment included in the same payroll." }}

UNQUANTIFIABLE AMOUNTS:
When a fact is real but has no quantifiable number in the message text (e.g. childcare payment with no amount stated), set new_value: null, confidence: "low", and note in reasoning that the amount is unspecified. NEVER emit placeholder strings like "childcare payment" into new_value.

SPECIAL: RELATIVE CHANGES
If the message describes a percentage change ("increased by 12%"), compute the absolute value using the most recent event amount and show the calculation.

LANGUAGE: The messages may be in English or Bahasa Indonesia. Read and understand them directly — do NOT translate.

OUTPUT FORMAT:
Return ONLY a valid JSON array of delta objects. No markdown fences, no conversational text.

Schema for each object:
If confirm_no_change:
{{
  "message_id": "<id>",
  "action": "confirm_no_change",
  "reasoning": "<brief explanation>"
}}

If amend, cancel, delay, or new_fact:
{{
  "message_id": "<id>",
  "action": "<amend|cancel|delay|new_fact>",
  "target": "<event_id or 'user_general'>",
  "field": "<amount|date|status|recurring_value>",
  "new_value": "<numeric string without currency codes, or null if unquantifiable>",
  "effective_date": "<YYYY-MM-DD or null if not specified>",
  "computation_note": "<show calculation if derived from relative change, else null>",
  "confidence": "<high|medium|low>",
  "source_language": "<en|id>",
  "reasoning": "<brief explanation>"
}}

MESSAGES TO CLASSIFY:
{messages_block}
"""


def extract_messages_batched(
    messages: List[Message],
    ds: DataStore,
    batch_size: int = 15,
) -> List[Dict[str, Any]]:
    """Classify messages in batches of size ~15-20.
    
    Checks per-message-id cache first. Only uncached messages are batched
    and sent to the LLM. Successfully classified messages are cached
    individually per message_id. Multi-clause messages emit multiple deltas.
    """
    results: Dict[str, List[Dict[str, Any]]] = {}
    uncached: List[Message] = []

    for msg in messages:
        cached = llm_client.get_cached("message_extraction", msg.message_id)
        if cached is not None:
            parsed = _parse_json_or_array_response(cached)
            if isinstance(parsed, list):
                results[msg.message_id] = [_build_message_result(msg, p) for p in parsed]
            else:
                results[msg.message_id] = [_build_message_result(msg, parsed)]
        else:
            uncached.append(msg)

    print(f"  [Batch Extraction] {len(results)}/{len(messages)} already cached. {len(uncached)} to process in batches of {batch_size}.", flush=True)

    # Process uncached in chunks
    for chunk_start in range(0, len(uncached), batch_size):
        chunk = uncached[chunk_start : chunk_start + batch_size]
        batch_num = (chunk_start // batch_size) + 1
        total_batches = (len(uncached) + batch_size - 1) // batch_size
        print(f"  Batch {batch_num}/{total_batches} ({len(chunk)} messages: {chunk[0].message_id}..{chunk[-1].message_id})...", end=" ", flush=True)

        msg_blocks = []
        for i, m in enumerate(chunk, 1):
            event_context = _get_event_context(m.related_event_id, ds)
            recent_context = _get_recent_events_for_user_category(m.user_id, m, ds)
            msg_blocks.append(
                f"--- MESSAGE {i}/{len(chunk)} ---\n"
                f"Message ID: {m.message_id}\n"
                f"User ID: {m.user_id}\n"
                f"Request ID: {m.request_id or '(none)'}\n"
                f"Related Event ID: {m.related_event_id or '(none)'}\n"
                f"Sent at: {m.sent_at.isoformat()}\n"
                f"Source type: {m.source_type}\n"
                f"Message text: {m.message_text}\n"
                f"Related Event Context: {event_context}\n"
                f"User's Recent Financial Events in Relevant Category:\n{recent_context}\n"
            )

        prompt = _BATCH_MESSAGE_PROMPT_TEMPLATE.format(
            count=len(chunk),
            messages_block="\n".join(msg_blocks),
        )

        target_id = f"batch_{chunk[0].message_id}_{chunk[-1].message_id}"
        try:
            raw = llm_client.call_text(
                prompt,
                call_type="message_batch",
                target_id=target_id,
                use_cache=False,
            )
            parsed_items = _parse_json_array_response(raw)

            # Group items by message_id
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            if isinstance(parsed_items, list):
                for item in parsed_items:
                    if isinstance(item, dict):
                        mid = item.get("message_id")
                        if mid:
                            grouped.setdefault(mid, []).append(item)

                if all(m.message_id in grouped for m in chunk):
                    for m in chunk:
                        items = grouped[m.message_id]
                        llm_client.set_cache("message_extraction", m.message_id, json.dumps(items))
                        results[m.message_id] = [_build_message_result(m, it) for it in items]
                    print(f"-> SUCCESS ({len(chunk)} messages processed)", flush=True)
                    continue

                if len(parsed_items) == len(chunk):
                    for m, item in zip(chunk, parsed_items):
                        if isinstance(item, dict):
                            item["message_id"] = m.message_id
                            llm_client.set_cache("message_extraction", m.message_id, json.dumps(item))
                            results[m.message_id] = [_build_message_result(m, item)]
                    print(f"-> SUCCESS (ordered {len(chunk)} classified)", flush=True)
                    continue

            # Fallback if array parsing or count didn't match:
            print("-> Warning: batch parse mismatch, falling back to per-message extraction for this batch...", flush=True)
            for m in chunk:
                try:
                    res = extract_message_delta(m, ds)
                    results[m.message_id] = res
                except Exception as ind_ex:
                    raise RuntimeError(f"Individual fallback extraction failed for message {m.message_id}: {ind_ex}") from ind_ex

        except Exception as ex:
            print(f"-> Batch failed ({ex}), falling back to individual calls...", flush=True)
            failed_mids: List[Tuple[str, str]] = []
            for m in chunk:
                try:
                    res = extract_message_delta(m, ds)
                    results[m.message_id] = res
                except Exception as inner_ex:
                    failed_mids.append((m.message_id, str(inner_ex)))

            if failed_mids:
                raise RuntimeError(
                    f"Message extraction failed closed for {len(failed_mids)} messages: {failed_mids}. "
                    "Failed extractions must never be converted to confirm_no_change."
                )

    # Verify all messages were successfully extracted
    missing_mids = [m.message_id for m in messages if not results.get(m.message_id)]
    if missing_mids:
        raise RuntimeError(f"Message extraction incomplete; missing results for {len(missing_mids)} messages: {missing_mids}")

    # Flatten the deltas list in message order
    flat_results: List[Dict[str, Any]] = []
    for m in messages:
        flat_results.extend(results.get(m.message_id, []))
    return flat_results


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Strip ``` or ```json code fences from LLM output."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


def _parse_json_response(raw: str) -> Dict[str, Any]:
    """Parse a single JSON object from an LLM response."""
    text = _strip_markdown_fences(raw)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {"error": "Failed to parse JSON", "raw": raw[:500]}


def _parse_json_array_response(raw: str) -> List[Dict[str, Any]]:
    """Parse a JSON array from an LLM response."""
    text = _strip_markdown_fences(raw)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Try finding bracketed array [ { ... }, ... ]
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return []


def _parse_json_or_array_response(raw: str) -> Any:
    """Parse JSON object or array from an LLM response or cache."""
    text = _strip_markdown_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match_arr = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match_arr:
        try:
            data = json.loads(match_arr.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    match_obj = re.search(r'\{.*\}', text, re.DOTALL)
    if match_obj:
        try:
            data = json.loads(match_obj.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return {"error": "Failed to parse JSON", "raw": raw[:500]}


# ---------------------------------------------------------------------------
# Validation of extraction data
# ---------------------------------------------------------------------------

ALLOWED_ACTIONS = {"confirm_no_change", "amend", "cancel", "delay", "new_fact"}


def validate_extraction_data(data: Dict[str, Any], ds: DataStore) -> None:
    """Validate cached or aggregate extraction data before use.

    Enforces:
    - exactly the expected blank-amount event/image IDs;
    - every one of the 215 message IDs represented by at least one valid delta;
    - no unknown message IDs;
    - only supported actions and required fields;
    - no extraction-failure placeholders;
    - duplicate deltas allowed only when they are valid multi-clause results.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Extraction data must be a dict, got {type(data)}")

    # 1. Validate Image Extractions
    blank_events = [e for e in ds.events if e.amount is None]
    expected_event_ids = {e.event_id for e in blank_events}
    image_extractions = data.get("image_extractions")
    if not isinstance(image_extractions, list):
        raise ValueError(f"'image_extractions' must be a list, got {type(image_extractions)}")

    extracted_event_ids = set()
    for idx, img in enumerate(image_extractions):
        if not isinstance(img, dict):
            raise ValueError(f"Image extraction {idx} is not a dict: {img}")
        ev_id = img.get("event_id")
        if not ev_id:
            raise ValueError(f"Image extraction {idx} missing event_id: {img}")
        if ev_id not in expected_event_ids:
            raise ValueError(f"Image extraction {idx} has unknown/unexpected event_id: {ev_id}")

        extracted_amount = img.get("extracted_amount")
        if extracted_amount is None:
            raise ValueError(f"Image extraction for {ev_id} missing extracted_amount")
        try:
            val = Decimal(str(extracted_amount))
            if val <= 0:
                raise ValueError(f"Image extraction for {ev_id} has non-positive amount: {val}")
        except (InvalidOperation, ValueError) as ex:
            raise ValueError(f"Image extraction for {ev_id} has invalid amount '{extracted_amount}': {ex}")

        # Check for failure placeholders
        for fld in ("reasoning", "confidence"):
            val_str = str(img.get(fld, "")).lower()
            if "extraction failed" in val_str or "failed to parse" in val_str:
                raise ValueError(f"Image extraction for {ev_id} contains failure placeholder in {fld}: {val_str}")

        extracted_event_ids.add(ev_id)

    if extracted_event_ids != expected_event_ids:
        missing = expected_event_ids - extracted_event_ids
        surplus = extracted_event_ids - expected_event_ids
        raise ValueError(f"Image extractions mismatch: missing={sorted(missing)}, surplus={sorted(surplus)}")

    # 2. Validate Message Deltas
    expected_mids = {m.message_id for m in ds.messages}
    message_deltas = data.get("message_deltas")
    if not isinstance(message_deltas, list):
        raise ValueError(f"'message_deltas' must be a list, got {type(message_deltas)}")

    seen_deltas_by_mid: Dict[str, List[Dict[str, Any]]] = {}
    for idx, delta in enumerate(message_deltas):
        if not isinstance(delta, dict):
            raise ValueError(f"Message delta {idx} is not a dict: {delta}")
        mid = delta.get("message_id")
        if not mid:
            raise ValueError(f"Message delta {idx} missing message_id: {delta}")
        if mid not in expected_mids:
            raise ValueError(f"Message delta {idx} has unknown message_id: {mid}")

        action = delta.get("action")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Message delta {idx} for {mid} has unsupported action: '{action}'")

        reasoning = str(delta.get("reasoning", "")).lower()
        if "extraction failed" in reasoning or "failed to parse" in reasoning:
            raise ValueError(f"Message delta {idx} for {mid} contains extraction-failure placeholder: '{delta.get('reasoning')}'")

        # Required fields per action
        if action == "confirm_no_change":
            if not str(delta.get("reasoning", "")).strip():
                raise ValueError(f"Message delta {idx} for {mid} (confirm_no_change) missing reasoning")
        elif action in ("amend", "cancel", "delay", "new_fact"):
            if not delta.get("target"):
                raise ValueError(f"Message delta {idx} for {mid} ({action}) missing target")
            if action == "amend":
                if delta.get("new_value") is None and delta.get("effective_date") is None:
                    raise ValueError(f"Message delta {idx} for {mid} (amend) missing amended new_value or effective_date")

        seen_deltas_by_mid.setdefault(mid, []).append(delta)

    extracted_mids = set(seen_deltas_by_mid.keys())
    if extracted_mids != expected_mids:
        missing = expected_mids - extracted_mids
        raise ValueError(f"Message extraction missing {len(missing)} message IDs: {sorted(missing)}")

    # Check multi-clause validity
    for mid, deltas in seen_deltas_by_mid.items():
        if len(deltas) > 1:
            signatures = [
                (
                    d.get("action"),
                    d.get("target"),
                    d.get("field"),
                    str(d.get("new_value")),
                    str(d.get("effective_date")),
                )
                for d in deltas
            ]
            if len(signatures) != len(set(signatures)):
                raise ValueError(f"Message {mid} contains duplicate identical delta clauses")

