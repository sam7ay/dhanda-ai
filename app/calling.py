"""Turns ledger rows into calls: live through Sarvam, or simulated for demos without a phone line."""
import json
import os
import random
import re
from datetime import date, datetime, timedelta, timezone

from . import db, ledger, sarvam_va

OUTCOMES = {
    "promised": "Promised to pay",
    "partial": "Partial payment promised",
    "already_paid": "Says already paid",
    "dispute": "Disputes amount",
    "callback": "Asked for callback",
    "refused": "Refused",
    "transferred": "Transferred to owner",
    "no_answer": "No answer",
    "busy": "Busy",
    "failed": "Call failed",
}
DEMO_MOBILE = "+919876543210"
CONTACTED = ("promised", "partial", "already_paid", "dispute", "callback", "refused", "transferred")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def webhook_secret():
    return os.environ.get("DHANDA_WEBHOOK_SECRET", "")


def _queue_call(retailer, business, dialed, mode, upsell, campaign_id=None, contact=None):
    variables = ledger.agent_variables(retailer, business, upsell=upsell)
    if business["owner_mobile"] == DEMO_MOBILE:
        variables["owner_mobile"] = ""  # never hand a real call to the demo account's placeholder number
    if contact and contact.get("name"):
        # A real person is answering a sample retailer's call, so greet them by their own name.
        variables["retailer_name"] = contact["name"].split()[0]
    call_id = db.execute(
        "INSERT INTO calls (campaign_id, retailer_id, dialed_number, mode, status, created_at, variables)"
        " VALUES (?,?,?,?,?,?,?)",
        (campaign_id, retailer["id"], dialed, mode, "queued", now(), json.dumps(variables, ensure_ascii=False)),
    )
    return call_id, variables


def _test_contacts(business):
    contacts = business["test_numbers"]
    if not contacts:
        raise ValueError("Add at least one test phone number in Settings first. "
                         "Real calls only ever go to your test phones.")
    return contacts


def public_base_url():
    """Where Sarvam posts call results. On Vercel this defaults to the production URL."""
    if os.environ.get("PUBLIC_BASE_URL"):
        return os.environ["PUBLIC_BASE_URL"]
    prod = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
    return f"https://{prod}" if prod else None


def launch_campaign(name, retailer_ids, window_start, window_end, upsell, live, every_day=False):
    business = db.business()
    summaries = {r["id"]: r for r in ledger.retailer_summaries()}
    targets = [summaries[i] for i in retailer_ids if i in summaries]
    if not targets:
        raise ValueError("Pick at least one retailer.")
    contacts = _test_contacts(business) if live else None
    if live:
        # One call per test phone: each person hears one retailer's reminder, never several.
        targets = targets[:len(contacts)]
    mode = "live" if live else "simulated"
    campaign_id = db.execute(
        "INSERT INTO campaigns (name, mode, status, created_at, window_start, window_end, upsell)"
        " VALUES (?,?,?,?,?,?,?)",
        (name, mode, "preparing", now(), window_start, window_end, int(upsell)),
    )
    queued = []
    for i, r in enumerate(targets):
        contact = contacts[i] if live else None
        dialed = contact["mobile"] if live else r["phone"]
        call_id, variables = _queue_call(r, business, dialed, mode, upsell, campaign_id, contact)
        queued.append((call_id, r, dialed, variables))

    if not live:
        for call_id, r, _, variables in queued:
            simulate(call_id, r, variables)
        db.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (campaign_id,))
        return campaign_id

    try:
        created = sarvam_va.create_campaign(
            f"{name} #{campaign_id}", window_start, window_end, days=7, every_day=every_day,
            public_base_url=public_base_url(),
            metadata={"dhanda_campaign": campaign_id, "tenant": db.tenant(), "token": webhook_secret()},
        )
        sarvam_id = created["campaign_id"]
        users = [{
            "user_phone_number": dialed,
            "user_identifier": f"{db.tenant()}-call-{call_id}",
            "app_variables": variables,
            "app_overrides": {"initial_language_name": r["language"]},
        } for call_id, r, dialed, variables in queued]
        cohort_ids = []
        for start in range(0, len(users), 1000):
            cohort = sarvam_va.stream_cohort(sarvam_id, f"dhanda-{campaign_id}-{start // 1000 + 1}",
                                             users[start:start + 1000])
            cohort_ids.append(cohort["cohort_id"])
        db.execute("UPDATE campaigns SET status = ?, sarvam_campaign_id = ?, sarvam_cohort_id = ? WHERE id = ?",
                   (created.get("status", "scheduled"), sarvam_id, ",".join(cohort_ids), campaign_id))
        db.execute("UPDATE calls SET status = 'scheduled' WHERE campaign_id = ?", (campaign_id,))
    except (sarvam_va.SarvamError, KeyError) as exc:
        db.execute("UPDATE campaigns SET status = 'failed', error = ? WHERE id = ?", (str(exc), campaign_id))
        db.execute("UPDATE calls SET status = 'failed', outcome = 'failed' WHERE campaign_id = ?", (campaign_id,))
    return campaign_id


def call_now(retailer_id, live):
    """One instant call to a single retailer (live calls go to the first test number)."""
    business = db.business()
    r = next(s for s in ledger.retailer_summaries() if s["id"] == retailer_id)
    if not live:
        call_id, variables = _queue_call(r, business, r["phone"], "simulated", True)
        simulate(call_id, r, variables)
        return call_id
    contact = _test_contacts(business)[0]
    dialed = contact["mobile"]
    call_id, variables = _queue_call(r, business, dialed, "live", True, contact=contact)
    try:
        resp = sarvam_va.instant_call(dialed, variables, r["language"],
                                      public_base_url=public_base_url(),
                                      metadata={"call_id": call_id, "tenant": db.tenant(), "token": webhook_secret()})
        db.execute("UPDATE calls SET status = 'dialing', sarvam_attempt_id = ? WHERE id = ?",
                   (resp["attempt_id"], call_id))
    except (sarvam_va.SarvamError, KeyError) as exc:
        db.execute("UPDATE calls SET status = 'failed', outcome = 'failed', summary = ? WHERE id = ?",
                   (str(exc), call_id))
    return call_id


def _amount(value):
    try:
        return float(str(value).replace("₹", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# The agent's own labels (as set up in Sarvam) mapped onto ours.
LABELS = {
    "payment_status": {"disputed": "dispute", "busy_callback": "callback", "no_contact": "callback"},
    "upsell_interest": {"interested": "yes", "not_interested": "no", "not_asked": "not_offered",
                        "no_opportunity": "not_offered"},
    "call_transferred": {"transferred": "yes", "owner_unavailable": "no", "not_needed": "no"},
}


def normalise_vars(variables):
    v = dict(variables or {})
    for key, mapping in LABELS.items():
        if key in v and isinstance(v[key], str):
            v[key] = mapping.get(v[key].strip().lower(), v[key].strip().lower())
    return v


def outcome_from(connectivity, variables, ended_by=None):
    variables = normalise_vars(variables)
    if connectivity in ("busy", "no_answer", "failed"):
        return connectivity
    if not connectivity:
        return None
    if ended_by == "CALL_TRANSFER" or str(variables.get("call_transferred", "")).lower() == "yes":
        return "transferred"
    outcome = variables.get("payment_status")
    return outcome if outcome in OUTCOMES else "callback"


def apply_result(call_id, *, connectivity, duration, language, variables, transcript, ended_by=None,
                 attempt_id=None, final=True):
    """Store one call outcome from a Sarvam webhook or the analytics API."""
    variables = normalise_vars(variables)
    outcome = outcome_from(connectivity, variables, ended_by)
    db.execute(
        """UPDATE calls SET status = ?, connectivity = ?, duration = ?, language = ?, outcome = ?,
               promised_amount = ?, promised_date = ?, upsell_interest = ?, products_interested = ?,
               transferred = ?, transcript = COALESCE(?, transcript), summary = COALESCE(?, summary),
               sarvam_attempt_id = COALESCE(?, sarvam_attempt_id)
           WHERE id = ?""",
        ("completed" if final else "retrying", connectivity, duration, language, outcome,
         _amount(variables.get("promised_amount")), variables.get("promised_date") or None,
         variables.get("upsell_interest") or None, variables.get("products_interested") or None,
         int(outcome == "transferred"),
         json.dumps(transcript, ensure_ascii=False) if transcript else None,
         variables.get("call_summary") or None, attempt_id, call_id),
    )


def sync_live_results():
    """Pull outcomes from Sarvam's analytics API (works without a public webhook URL)."""
    pending = db.rows("SELECT id, created_at FROM calls WHERE mode = 'live' AND status NOT IN ('completed', 'failed')")
    if not pending:
        return 0
    since = min(datetime.fromisoformat(c["created_at"]) for c in pending) - timedelta(minutes=5)
    wanted = {f"{db.tenant()}-call-{c['id']}": c["id"] for c in pending}
    by_attempt = {a["sarvam_attempt_id"]: a["id"] for a in
                  db.rows("SELECT id, sarvam_attempt_id FROM calls WHERE sarvam_attempt_id IS NOT NULL")}
    updated = 0
    for item in sarvam_va.list_attempts(since):
        call_id = wanted.get(item.get("user_identifier")) or by_attempt.get(item.get("attempt_id"))
        if not call_id:
            continue
        transcript = None
        if item.get("connectivity_status") == "connected" and item.get("interaction_id"):
            try:
                transcript = sarvam_va.get_transcript(item["interaction_id"])
            except sarvam_va.SarvamError:
                transcript = None
        apply_result(call_id, connectivity=item.get("connectivity_status"),
                     duration=item.get("duration_in_seconds"), language=item.get("language_name"),
                     variables=item.get("agent_variables"), transcript=transcript,
                     ended_by=item.get("ended_by"), attempt_id=item.get("attempt_id"),
                     final=item.get("next_action_status") in (None, "UNKNOWN"))
        updated += 1
    return updated


def handle_webhook(payload):
    meta = payload.get("metadata") or {}
    if webhook_secret() and meta.get("token") != webhook_secret():
        return False
    ident = str(payload.get("user_identifier") or "")
    tenant, _, num = ident.partition("-call-")
    tenant = meta.get("tenant") or tenant
    call_id = meta.get("call_id") or (int(num) if num.isdigit() else None)
    if not call_id or not tenant or not re.fullmatch(r"[a-z0-9]{1,20}", tenant):
        return False
    db.use_tenant(tenant)
    if not db.db_path().exists():
        return False
    transcript = [{"role": t.get("role"), "text": t.get("en_text")} for t in payload.get("interaction_transcript") or []]
    variables = {**(payload.get("final_agent_variables") or {}), **(payload.get("output_agent_variables") or {})}
    apply_result(int(call_id), connectivity=payload.get("connectivity_status"), duration=payload.get("duration"),
                 language=None, variables=variables, transcript=transcript or None,
                 attempt_id=payload.get("attempt_id"), final=payload.get("next_action_status") is None)
    return True


# ---------- Simulation (demo without a phone line) ----------

def simulate(call_id, r, v):
    rnd = random.Random(call_id * 7919)
    if rnd.random() < 0.12:
        apply_result(call_id, connectivity=rnd.choice(["no_answer", "busy"]), duration=None,
                     language=None, variables={}, transcript=None)
        return
    days = r["max_days_overdue"]
    weights = {"promised": 40, "partial": 18, "callback": 12, "already_paid": 6, "dispute": 5,
               "refused": 4, "transferred": 6}
    if days > 60:
        weights.update(promised=22, partial=24, refused=10, dispute=8, transferred=10)
    if r["overdue"] == 0:
        weights.update(promised=55, partial=5, dispute=2, refused=1)
    outcome = rnd.choices(list(weights), weights=list(weights.values()))[0]
    pay_by = date.today() + timedelta(days=rnd.choice([1, 2, 3, 5, 7]))
    overdue = r["overdue"] or r["outstanding"]
    promised = {"promised": overdue, "partial": round(overdue * rnd.choice([0.3, 0.4, 0.5]), -2)}.get(outcome)
    pitch = v["upsell_allowed"] in ("true", "yes") and outcome in ("promised", "partial", "already_paid")
    interest = rnd.choices(["yes", "maybe", "no"], weights=[35, 30, 35])[0] if pitch else "not_offered"
    products = v["reorder_products"] if v["reorder_products"] != "none" else v["recommended_products"]
    lines = _script(r, v, outcome, promised, pay_by, pitch, interest, products)
    variables = {
        "payment_status": outcome if outcome != "transferred" else "callback",
        "call_transferred": "yes" if outcome == "transferred" else "no",
        "promised_amount": str(promised) if promised else "",
        "promised_date": pay_by.isoformat() if promised else "",
        "upsell_interest": interest,
        "products_interested": products if interest in ("yes", "maybe") else "",
        "call_summary": lines[-1][1] if outcome == "dispute" else "",
    }
    apply_result(call_id, connectivity="connected", duration=round(rnd.uniform(45, 170), 1),
                 language=r["language"], variables=variables,
                 transcript=[{"role": role, "text": text} for role, text in lines])


def _script(r, v, outcome, promised, pay_by, pitch, interest, products):
    greet = "Sat Sri Akal" if r["language"] == "Punjabi" else "Namaste"
    when = pay_by.strftime("%A")
    lines = [
        ("agent", f"{greet} {v['retailer_name']} ji, I'm calling from {v['business_name']} on behalf of "
                  f"{v['owner_name']}. Is this a good time for two minutes?"),
        ("user", "Yes, go ahead."),
        ("agent", f"Your account shows {v['outstanding_amount']} outstanding, and {v['overdue_amount']} of it "
                  f"was due on {v['oldest_due_date']}. When can you clear it?"),
    ]
    user_reply = {
        "promised": f"Okay, I'll send the full amount by {when}.",
        "partial": f"Business is slow this month. I can pay {ledger.inr(promised or 0)} by {when}, the rest next month.",
        "already_paid": "I already paid by UPI last week. Please check with the office.",
        "dispute": "That amount is wrong. Two cartons from the last bill were damaged and I sent them back.",
        "callback": "I'm with customers right now. Call me tomorrow evening.",
        "refused": "I can't commit to anything right now.",
        "transferred": f"I want to talk to {v['owner_name']} directly about this.",
    }[outcome]
    lines.append(("user", user_reply))
    agent_reply = {
        "promised": f"Thank you. I've noted {v['overdue_amount']} by {when}.",
        "partial": f"Understood. I've noted {ledger.inr(promised or 0)} by {when}. {v['owner_name']} ji will see this.",
        "already_paid": "Thank you, I'll flag it so the office can match your payment.",
        "dispute": "I'm sorry about that. I've recorded the damaged stock so the office can correct the bill.",
        "callback": "No problem, we'll call you tomorrow evening.",
        "refused": "I understand. I'll let the office know, and they may follow up.",
        "transferred": f"Of course, connecting you to {v['owner_name']} ji now.",
    }[outcome]
    lines.append(("agent", agent_reply))
    if pitch:
        lines.append(("agent", f"One more thing: you usually stock {products}. Shall I tell the office to send some with your next order?"))
        lines.append(("user", {"yes": "Yes, send it with the next delivery.",
                               "maybe": "Ask me next week, I'll check my stock.",
                               "no": "Not right now."}[interest]))
    return lines
