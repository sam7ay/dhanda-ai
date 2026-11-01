"""Outstanding credit, ageing and product recommendations, computed from the ledger."""
import re
from collections import Counter, defaultdict
from datetime import date

from . import db

BUCKETS = [("Not due", 0), ("1–30 days", 30), ("31–60 days", 60), ("61–90 days", 90), ("90+ days", None)]


def bucket(days_overdue):
    for label, upto in BUCKETS:
        if upto is None or days_overdue <= upto:
            return label


def _days_overdue(due_date, today):
    return (today - date.fromisoformat(due_date)).days


def retailer_summaries():
    today = date.today()
    retailers = {r["id"]: r for r in db.rows("SELECT * FROM retailers ORDER BY id")}
    for r in retailers.values():
        r.update(outstanding=0.0, overdue=0.0, oldest_due=None, max_days_overdue=0,
                 open_invoices=0, last_payment=None, last_payment_amount=0, last_call=None)
    for inv in db.rows("SELECT * FROM invoices WHERE amount > paid ORDER BY due_date"):
        r = retailers[inv["retailer_id"]]
        balance = inv["amount"] - inv["paid"]
        r["outstanding"] += balance
        r["open_invoices"] += 1
        days = _days_overdue(inv["due_date"], today)
        if days > 0:
            r["overdue"] += balance
            r["max_days_overdue"] = max(r["max_days_overdue"], days)
            r["oldest_due"] = r["oldest_due"] or inv["due_date"]
    for p in db.rows("SELECT retailer_id, date, amount FROM payments ORDER BY date"):
        r = retailers[p["retailer_id"]]
        r["last_payment"], r["last_payment_amount"] = p["date"], p["amount"]
    for c in db.rows("SELECT retailer_id, created_at, outcome, status FROM calls ORDER BY created_at"):
        retailers[c["retailer_id"]]["last_call"] = c
    for r in retailers.values():
        r["utilisation"] = r["outstanding"] / r["credit_limit"] if r["credit_limit"] else 0
        r["risk"] = _risk(r)
    return list(retailers.values())


def _risk(r):
    if r["max_days_overdue"] > 90 or r["utilisation"] > 1:
        return "High"
    if r["max_days_overdue"] > 45 or r["utilisation"] > 0.8:
        return "Medium"
    return "Low"


def ageing():
    today = date.today()
    totals = {label: 0.0 for label, _ in BUCKETS}
    for inv in db.rows("SELECT due_date, amount, paid FROM invoices WHERE amount > paid"):
        totals[bucket(_days_overdue(inv["due_date"], today))] += inv["amount"] - inv["paid"]
    return totals


def open_invoices(retailer_id):
    today = date.today()
    invs = db.rows("SELECT * FROM invoices WHERE retailer_id = ? AND amount > paid ORDER BY due_date",
                   (retailer_id,))
    for inv in invs:
        inv["balance"] = inv["amount"] - inv["paid"]
        inv["days_overdue"] = max(0, _days_overdue(inv["due_date"], today))
    return invs


def purchase_history(retailer_id):
    return db.rows(
        """SELECT s.id, s.code, s.name, s.name_hi, s.category, COUNT(*) AS times,
                  SUM(l.qty) AS qty, SUM(l.qty * l.rate) AS value, MAX(i.date) AS last_bought
           FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id JOIN skus s ON s.id = l.sku_id
           WHERE i.retailer_id = ? GROUP BY s.id ORDER BY value DESC""",
        (retailer_id,),
    )


def recommendations(retailer_id, limit=3):
    """Reorders: regulars that are past their usual gap. Cross-sells: what similar shops buy."""
    today = date.today()
    lines = db.rows(
        """SELECT i.retailer_id, l.sku_id, i.date FROM invoice_lines l
           JOIN invoices i ON i.id = l.invoice_id ORDER BY i.date"""
    )
    bought = defaultdict(lambda: defaultdict(list))
    for ln in lines:
        bought[ln["retailer_id"]][ln["sku_id"]].append(date.fromisoformat(ln["date"]))
    skus = {s["id"]: s for s in db.rows("SELECT * FROM skus")}
    mine = bought.get(retailer_id, {})

    reorders = []
    for sku_id, dates in mine.items():
        if len(dates) < 3:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        usual = sum(gaps) / len(gaps)
        since = (today - dates[-1]).days
        if since > usual:
            reorders.append((since / usual, dict(skus[sku_id], reason=f"usually every {usual:.0f} days, last {since} days ago")))
    reorders = [s for _, s in sorted(reorders, key=lambda x: -x[0])][:limit]

    # Shops sharing at least half of this shop's regular items count as similar.
    my_items = set(mine)
    scores = Counter()
    for other, items in bought.items():
        if other == retailer_id or not my_items:
            continue
        overlap = len(my_items & set(items)) / len(my_items)
        if overlap >= 0.5:
            for sku_id in set(items) - my_items:
                scores[sku_id] += overlap
    cross = [dict(skus[s], reason="popular with similar shops") for s, _ in scores.most_common(limit)]
    return {"reorder": reorders, "cross_sell": cross}


def upsell_allowed(summary):
    """Don't pitch new stock to a shop that is over its limit or badly overdue."""
    return summary["utilisation"] <= 1 and summary["max_days_overdue"] <= 60


def normalise_mobile(raw):
    """Indian mobile in +91XXXXXXXXXX form, or None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return "+" + digits
    return None


def parse_contacts(text):
    """'Rakesh, 98765 43210' per line (name optional) → [{"name", "mobile"}]. Raises ValueError on a bad line."""
    contacts = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"(\+?[\d][\d\s-]{8,}\d)", line)
        mobile = normalise_mobile(match.group(1)) if match else None
        if not mobile:
            raise ValueError(f"'{line}' doesn't have a valid Indian mobile number.")
        name = (line[:match.start()] + line[match.end():]).strip(" ,;:-\t")
        if mobile not in {c["mobile"] for c in contacts}:
            contacts.append({"name": name, "mobile": mobile})
    return contacts


def inr(value):
    value = round(value)
    s = str(abs(value))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if value < 0 else "") + "₹" + s


def agent_variables(summary, business, upsell=True):
    """The per-call variables the Sarvam agent reads. Keys must match the agent's variables."""
    recs = recommendations(summary["id"]) if upsell and upsell_allowed(summary) else {"reorder": [], "cross_sell": []}
    return {
        "retailer_name": summary["owner_name"].split()[0],
        "shop_name": summary["shop_name"],
        "business_name": business["name"],
        "owner_name": business["owner_name"],
        "owner_mobile": business["owner_mobile"],
        "outstanding_amount": inr(summary["outstanding"]),
        "overdue_amount": inr(summary["overdue"]),
        "oldest_due_date": date.fromisoformat(summary["oldest_due"]).strftime("%d %B") if summary["oldest_due"] else "none",
        "days_overdue": str(summary["max_days_overdue"]),
        "last_payment": (f"{inr(summary['last_payment_amount'])} on "
                         f"{date.fromisoformat(summary['last_payment']).strftime('%d %B')}") if summary["last_payment"] else "none",
        "reorder_products": "; ".join(s["name_hi"] for s in recs["reorder"]) or "none",
        "recommended_products": "; ".join(s["name_hi"] for s in recs["cross_sell"]) or "none",
        "upsell_allowed": "yes" if (recs["reorder"] or recs["cross_sell"]) else "no",
    }
