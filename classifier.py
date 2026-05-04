"""
Gemini-based email classifier for gmail-cleanup.

classify_emails() is the main entry point. It:
1. Pre-classifies emails from known contacts as KEEP (no API call needed).
2. Sends remaining emails to Gemini 2.5 Flash in batches of 25.
3. Validates the response and falls back to KEEP on any error.
4. Saves all classifications to the database.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google import genai
from google.genai import types

from config import (
    CLASSIFY_BATCH_SIZE,
    CLASSIFY_SYSTEM_PROMPT,
    GEMINI_MODEL,
    Category,
)
from db import DB


def _build_email_repr(email: dict, sent_count: int) -> dict:
    """Build the JSON-serialisable dict that is sent to Gemini for each email.

    Only includes fields that help with classification; no email body is sent.
    The sent_count_hint tells Gemini that you have corresponded with this
    sender before, nudging it towards KEEP for borderline cases.
    """
    rep = {
        "uid": email["uid"],
        "from": email["from_addr"],
        "subject": email["subject"],
        "date": email["date"],
        "has_list_unsubscribe": bool(email.get("list_unsub")),
        "reply_to": email.get("reply_to") or None,
    }
    if sent_count > 0:
        rep["sent_count_hint"] = sent_count
    return rep


def _call_gemini(client: genai.Client, model: str, emails: list[dict]) -> list[dict]:
    """Send a batch of email representations to Gemini and return the classifications.

    Uses structured output (response_schema) so the model is forced to return
    valid JSON matching our schema. On any error, returns an empty list and lets
    the caller fall back to KEEP for unhandled UIDs.
    """
    payload = json.dumps(emails, ensure_ascii=False)
    prompt = f"{CLASSIFY_SYSTEM_PROMPT}\n\nEmails to classify:\n{payload}"

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uid": {"type": "integer"},
                            "category": {
                                "type": "string",
                                "enum": [c.value for c in Category],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["uid", "category", "reason"],
                    },
                },
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"  [WARN] Gemini API error: {e}")
        return []


def classify_emails(
    db: DB,
    api_key: str,
    limit: Optional[int] = None,
    reclassify: bool = False,
    model: Optional[str] = None,
) -> int:
    """Classify unclassified emails and save results to the database.

    Args:
        db:          Database instance.
        api_key:     Google AI API key.
        limit:       Max number of emails to classify in this run.
        reclassify:  If True, drop non-overridden classifications first and redo them.
        model:       Gemini model name. Falls back to GEMINI_MODEL from config.

    Returns:
        Total number of emails classified in this run.
    """
    model = model or GEMINI_MODEL
    client = genai.Client(api_key=api_key)

    if reclassify:
        # Only remove auto-classifications; keep user overrides (overridden=1)
        db.conn.execute("DELETE FROM classifications WHERE overridden=0")
        db.conn.commit()

    unclassified = db.get_unclassified(limit=limit)
    if not unclassified:
        print("  No unclassified emails found.")
        return 0

    print(f"  {len(unclassified)} emails to classify")

    # ── Step 1: pre-classify known contacts as KEEP (free, no API call) ──────
    contact_results = []
    to_classify = []

    for email_row in unclassified:
        sent_count = db.get_contact_sent_count(email_row["from_addr"])
        if db.is_contact(email_row["from_addr"]):
            contact_results.append({
                "uid": email_row["uid"],
                "category": Category.KEEP.value,
                "reason": f"Known contact (sent {sent_count}x)",
                "overridden": 0,
            })
        else:
            to_classify.append((email_row, sent_count))

    if contact_results:
        db.save_classifications(contact_results)
        print(f"  {len(contact_results)} pre-classified as KEEP (known contacts)")

    total_classified = len(contact_results)

    # ── Step 2: classify remaining emails via Gemini ──────────────────────────
    batch_emails = [e for e, _ in to_classify]
    batch_counts = [c for _, c in to_classify]

    batches = [
        (i, batch_emails[i:i + CLASSIFY_BATCH_SIZE], batch_counts[i:i + CLASSIFY_BATCH_SIZE])
        for i in range(0, len(batch_emails), CLASSIFY_BATCH_SIZE)
    ]
    total_batches = len(batches)
    completed = 0

    def _process_batch(args):
        i, batch, counts = args
        reprs = [_build_email_repr(e, c) for e, c in zip(batch, counts)]
        results = _call_gemini(client, model, reprs)
        result_map = {r["uid"]: r for r in results}
        classifications = []
        for email_row in batch:
            uid = email_row["uid"]
            if uid in result_map:
                r = result_map[uid]
                cat = r["category"]
                reason = r.get("reason", "")
            else:
                cat = Category.KEEP.value
                reason = "Not returned by classifier, defaulting to KEEP"
            if cat not in [c.value for c in Category]:
                cat = Category.KEEP.value
            classifications.append({"uid": uid, "category": cat, "reason": reason, "overridden": 0})
        return i, classifications

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_process_batch, b): b for b in batches}
        for future in as_completed(futures):
            i, classifications = future.result()
            db.save_classifications(classifications)
            total_classified += len(classifications)
            completed += 1
            print(f"  {total_classified - len(contact_results)}/{len(batch_emails)} classified via Gemini ({completed}/{total_batches} batches)", end="\r", flush=True)

    print()
    return total_classified
