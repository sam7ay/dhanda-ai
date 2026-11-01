"""Imports Busy / Marg report exports (Excel or CSV) into the ledger.

Three reports, matched by column names rather than fixed positions, because Busy and Marg
exports differ by version and by how the report was set up:

- bills:   bill-wise outstanding / receivables (required)
- sales:   item-wise sales register, for purchase history and suggestions (optional)
- parties: account / party master list, for mobile numbers, city and credit limit (optional)
"""
import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, ledger

FIELDS = {
    "bills": {
        "party": ("Party", True, ["party name", "party", "account name", "account", "customer name", "customer",
                                  "ledger name", "ledger", "name of party", "particulars", "name"]),
        "bill_no": ("Bill no.", True, ["bill no", "bill number", "invoice no", "invoice number", "vch no",
                                       "voucher no", "vch bill no", "ref no", "bill ref", "reference no", "inv no", "bill"]),
        "bill_date": ("Bill date", True, ["bill date", "invoice date", "vch date", "voucher date", "inv date", "date"]),
        "due_date": ("Due date", False, ["due date", "due on", "due"]),
        "bill_amount": ("Bill amount", False, ["bill amt", "bill amount", "invoice amount", "original amount",
                                               "vch amount", "total amount", "net amount", "amount"]),
        "pending": ("Pending amount", True, ["pending amt", "pending amount", "pending", "balance amount", "balance",
                                             "bal amt", "outstanding amount", "outstanding", "os amount", "due amount",
                                             "closing balance"]),
        "mobile": ("Mobile", False, ["mobile no", "mobile", "phone no", "phone", "contact no", "mob no", "whatsapp no"]),
        "city": ("City", False, ["city", "station", "town", "area", "place"]),
    },
    "sales": {
        "date": ("Date", True, ["date", "bill date", "invoice date", "vch date", "voucher date"]),
        "bill_no": ("Bill no.", False, ["bill no", "bill number", "invoice no", "vch no", "voucher no",
                                        "vch bill no", "inv no", "ref no"]),
        "party": ("Party", True, ["party name", "party", "account name", "account", "customer name", "customer",
                                  "ledger name"]),
        "item": ("Item", True, ["item name", "item", "product name", "product", "item description", "description",
                                "particulars"]),
        "qty": ("Quantity", True, ["qty", "quantity", "qnty", "main qty", "billed qty"]),
        "rate": ("Rate", False, ["rate", "price", "unit price", "sale rate"]),
        "amount": ("Amount", False, ["amount", "value", "net amount", "taxable amount", "total"]),
    },
    "parties": {
        "party": ("Party", True, ["party name", "party", "account name", "account", "name", "customer name",
                                  "ledger name", "customer"]),
        "mobile": ("Mobile", False, ["mobile no", "mobile", "phone no", "phone", "contact no", "mob no", "whatsapp no"]),
        "city": ("City", False, ["city", "station", "town", "area", "place"]),
        "credit_limit": ("Credit limit", False, ["credit limit", "cr limit", "limit", "credit amount"]),
        "language": ("Language", False, ["language", "lang"]),
    },
}
REPORT_NAMES = {"bills": "Bill-wise outstanding", "sales": "Item-wise sales", "parties": "Party list"}

PUNJAB_CITIES = {
    "ludhiana", "jalandhar", "amritsar", "patiala", "bathinda", "moga", "khanna", "phagwara", "mohali",
    "hoshiarpur", "pathankot", "barnala", "sangrur", "firozpur", "ferozepur", "kapurthala", "faridkot",
    "muktsar", "fazilka", "rajpura", "batala", "gurdaspur", "malerkotla", "nawanshahr", "ropar", "rupnagar",
    "mansa", "zirakpur", "kharar", "tarn taran", "abohar", "jagraon", "samrala", "nabha", "sunam",
}
DEFAULT_CREDIT_DAYS = 30
UPLOAD_DIR = db.DB_PATH.parent / "uploads"


class ImportError_(Exception):
    pass


# ---------- Reading files ----------

def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def read_table(path):
    """All rows of the first sheet (xlsx) or the CSV, as lists of cell values."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        table = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return table
    if suffix in (".csv", ".txt"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|") if text.strip() else csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect)]
    if suffix == ".xls":
        raise ImportError_("Old .xls files aren't supported. In Excel, use Save As → .xlsx (or export the report as CSV).")
    raise ImportError_(f"Unsupported file type {suffix}. Upload .xlsx or .csv.")


def _match_score(header, synonyms):
    h = _norm(header)
    if not h:
        return 0
    for i, s in enumerate(synonyms):
        if h == s:
            return 100 - i
    for i, s in enumerate(synonyms):
        if len(s) > 3 and (h.startswith(s) or s in h.split(" ") or s in h):
            return 50 - i
    return 0


def detect(table, kind):
    """Find the header row (reports often start with company name and title lines) and map columns."""
    fields = FIELDS[kind]
    best = (0, None)
    for i, row in enumerate(table[:25]):
        hits = sum(1 for cell in row for _, _, syn in fields.values() if _match_score(cell, syn) >= 50)
        if hits > best[0]:
            best = (hits, i)
    if best[1] is None or best[0] < 2:
        raise ImportError_(f"Couldn't find a header row in the {REPORT_NAMES[kind].lower()} file. "
                           "Make sure the export includes column titles.")
    header_row = best[1]
    headers = [str(c).strip() if c is not None else "" for c in table[header_row]]
    mapping, taken = {}, set()
    # Strongest matches first, so "Bill Date" wins "bill_date" before "date"-like fields grab it.
    candidates = sorted(
        ((_match_score(h, syn), field, col) for field, (_, _, syn) in fields.items() for col, h in enumerate(headers)),
        reverse=True,
    )
    for score, field, col in candidates:
        if score < 50 or field in mapping or col in taken:
            continue
        mapping[field] = col
        taken.add(col)
    return header_row, headers, mapping


# ---------- Parsing values ----------

DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%d-%b-%Y", "%d-%b-%y",
                "%d %b %Y", "%d %b %y", "%d-%B-%Y", "%d %B %Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S"]


def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:  # Excel serial date
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value):
    """'1,23,456.00 Dr' → 123456.0, '500 Cr' → -500.0, '(500)' → -500.0."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")") or text.lower().endswith("cr") or text.startswith("-")
    digits = re.sub(r"[^0-9.]", "", text)
    if not digits or digits == ".":
        return None
    try:
        amount = float(digits)
    except ValueError:
        return None
    return -amount if negative else amount


def _cell(row, mapping, field):
    col = mapping.get(field)
    if col is None or col >= len(row):
        return None
    value = row[col]
    return value.strip() if isinstance(value, str) else value


def _party_key(name):
    return _norm(name)


def _is_total(text):
    t = _norm(text)
    return t.startswith(("total", "grand total", "sub total", "subtotal")) or t in ("carried over", "brought forward")


# ---------- Building the import ----------

def parse_bills(table, header_row, mapping):
    bills, warnings, current_party = [], [], None
    skipped_credit = 0
    for n, row in enumerate(table[header_row + 1:], start=header_row + 2):
        party = _cell(row, mapping, "party")
        bill_no = _cell(row, mapping, "bill_no")
        pending = parse_amount(_cell(row, mapping, "pending"))
        if party and _is_total(party):
            continue
        # Busy's bill-wise report puts the party on its own line with bills underneath.
        if party and not bill_no and pending in (None, 0):
            current_party = str(party)
            continue
        party = str(party) if party else current_party
        if not party or not bill_no or pending is None:
            continue
        if pending <= 0:
            skipped_credit += 1
            continue
        bill_date = parse_date(_cell(row, mapping, "bill_date"))
        if not bill_date:
            warnings.append(f"Row {n}: couldn't read the bill date for {bill_no}, skipped.")
            continue
        due = parse_date(_cell(row, mapping, "due_date")) or bill_date + timedelta(days=DEFAULT_CREDIT_DAYS)
        amount = parse_amount(_cell(row, mapping, "bill_amount"))
        bills.append({
            "party": party.strip(), "bill_no": str(bill_no).strip(), "bill_date": bill_date, "due_date": due,
            "amount": max(amount or 0, pending), "pending": pending,
            "mobile": ledger.normalise_mobile(_cell(row, mapping, "mobile")),
            "city": (str(_cell(row, mapping, "city") or "").strip() or None),
        })
    if skipped_credit:
        warnings.append(f"{skipped_credit} credit or advance entries (Cr balances) were skipped.")
    if "due_date" not in mapping:
        warnings.append(f"No due date column, so bills are treated as due {DEFAULT_CREDIT_DAYS} days after the bill date.")
    return bills, warnings


def parse_sales(table, header_row, mapping):
    lines, warnings = [], []
    current_party, current_date, current_bill = None, None, None
    for row in table[header_row + 1:]:
        party = _cell(row, mapping, "party")
        item = _cell(row, mapping, "item")
        if (party and _is_total(party)) or (item and _is_total(item)):
            continue
        d = parse_date(_cell(row, mapping, "date"))
        bill = _cell(row, mapping, "bill_no")
        # Item-wise registers often fill party, date and bill only on a bill's first line.
        if party:
            current_party = str(party).strip()
        if d:
            current_date = d
        if bill:
            current_bill = str(bill).strip()
        qty = parse_amount(_cell(row, mapping, "qty"))
        if not item or not qty or qty <= 0 or not current_party or not current_date:
            continue
        rate = parse_amount(_cell(row, mapping, "rate"))
        amount = parse_amount(_cell(row, mapping, "amount"))
        if not rate and amount:
            rate = amount / qty
        lines.append({"party": current_party, "date": current_date, "bill_no": current_bill,
                      "item": str(item).strip(), "qty": qty, "rate": rate or 0})
    if "rate" not in mapping and "amount" not in mapping:
        warnings.append("No rate or amount column in the sales file, so purchase values show as ₹0.")
    return lines, warnings


def parse_parties(table, header_row, mapping):
    parties = {}
    for row in table[header_row + 1:]:
        name = _cell(row, mapping, "party")
        if not name or _is_total(name):
            continue
        language = str(_cell(row, mapping, "language") or "").strip().capitalize()
        parties[_party_key(name)] = {
            "mobile": ledger.normalise_mobile(_cell(row, mapping, "mobile")),
            "city": (str(_cell(row, mapping, "city") or "").strip() or None),
            "credit_limit": parse_amount(_cell(row, mapping, "credit_limit")) or 0,
            "language": language if language in ("Hindi", "Punjabi") else None,
        }
    return parties


def save_upload(token, kind, filename, data):
    folder = UPLOAD_DIR / token
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{kind}.*"):
        old.unlink()
    suffix = Path(filename).suffix.lower() or ".csv"
    path = folder / f"{kind}{suffix}"
    path.write_bytes(data)
    return path


def uploaded(token):
    folder = UPLOAD_DIR / token
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token or "") or not folder.exists():
        return {}
    return {p.stem: p for p in folder.iterdir() if p.stem in FIELDS}


def analyse(token, overrides=None):
    """Reads the uploaded files, maps columns (auto, then any user overrides) and dry-runs the import."""
    overrides = overrides or {}
    files = uploaded(token)
    if "bills" not in files:
        raise ImportError_("Upload the bill-wise outstanding report. It's the one file that's required.")
    report = {"token": token, "files": {}, "warnings": [], "errors": []}
    parsed = {}
    for kind, path in files.items():
        try:
            table = read_table(path)
            header_row, headers, mapping = detect(table, kind)
        except ImportError_ as exc:
            report["errors"].append(f"{REPORT_NAMES[kind]}: {exc}")
            continue
        for field in FIELDS[kind]:
            key = f"{kind}.{field}"
            if key in overrides:
                if overrides[key] == "":
                    mapping.pop(field, None)
                else:
                    mapping[field] = int(overrides[key])
        missing = [FIELDS[kind][f][0] for f, (_, req, _) in FIELDS[kind].items() if req and f not in mapping]
        if missing:
            report["errors"].append(f"{REPORT_NAMES[kind]}: pick a column for {', '.join(missing)}.")
        body = [r for r in table[header_row + 1:] if any(c not in (None, "") for c in r)]
        report["files"][kind] = {
            "name": REPORT_NAMES[kind], "filename": path.name, "headers": headers, "mapping": mapping,
            "fields": FIELDS[kind], "header_row": header_row + 1, "rows": len(body),
            "sample": [[r[i] if i < len(r) else None for i in range(len(headers))] for r in body[:5]],
        }
        parsed[kind] = (table, header_row, mapping, missing)

    if report["errors"]:
        return report, None

    bills, warns = parse_bills(*parsed["bills"][:3])
    report["warnings"] += warns
    lines = []
    if "sales" in parsed:
        lines, warns = parse_sales(*parsed["sales"][:3])
        report["warnings"] += warns
    parties = parse_parties(*parsed["parties"][:3]) if "parties" in parsed else {}

    names = {}
    for b in bills:
        names.setdefault(_party_key(b["party"]), b["party"])
    unmatched_sales = {l["party"] for l in lines if _party_key(l["party"]) not in names}
    for p in unmatched_sales:
        names.setdefault(_party_key(p), p)  # shops with purchases but nothing outstanding
    no_mobile = []
    for key, name in names.items():
        mob = (parties.get(key) or {}).get("mobile") or next((b["mobile"] for b in bills if _party_key(b["party"]) == key and b["mobile"]), None)
        if not mob:
            no_mobile.append(name)
    if not bills:
        report["errors"].append("No outstanding bills were found. Check the column mapping below.")
    if no_mobile:
        report["warnings"].append(f"{len(no_mobile)} of {len(names)} retailers have no mobile number"
                                  + ("" if "parties" in parsed else " (upload the party list to add them)") + ".")
    report["summary"] = {
        "retailers": len(names), "bills": len(bills), "pending": sum(b["pending"] for b in bills),
        "sales_lines": len(lines), "items": len({l["item"].lower() for l in lines}),
        "with_mobile": len(names) - len(no_mobile),
        "date_range": (min((b["bill_date"] for b in bills), default=None), max((b["bill_date"] for b in bills), default=None)),
    }
    return report, {"bills": bills, "lines": lines, "parties": parties, "names": names}


def guess_language(city):
    return "Punjabi" if city and _norm(city) in PUNJAB_CITIES else "Hindi"


def commit(data, erp):
    """Replaces the sample book with the imported one. Calls and campaigns on the old book are cleared."""
    bills, lines, parties, names = data["bills"], data["lines"], data["parties"], data["names"]
    today = date.today()
    sales_value = {}
    for l in lines:
        k = _party_key(l["party"])
        sales_value[k] = sales_value.get(k, 0) + l["qty"] * l["rate"]
    ranked = sorted(sales_value.values())

    def segment(key):
        v = sales_value.get(key, 0)
        if not ranked or v == 0:
            return "Unrated"
        pos = ranked.index(v) / max(1, len(ranked) - 1)
        return "Large" if pos >= 0.67 else "Medium" if pos >= 0.33 else "Small"

    with db.tx() as conn:
        for table in ("calls", "campaigns", "payments", "invoice_lines", "invoices", "skus", "retailers"):
            conn.execute(f"DELETE FROM {table}")
        retailer_ids = {}
        for i, (key, name) in enumerate(sorted(names.items(), key=lambda kv: kv[1].lower())):
            info = parties.get(key, {})
            first_bill = next((b for b in bills if _party_key(b["party"]) == key), {})
            city = info.get("city") or first_bill.get("city") or ""
            mobile = info.get("mobile") or next((b["mobile"] for b in bills if _party_key(b["party"]) == key and b["mobile"]), None)
            retailer_ids[key] = conn.execute(
                "INSERT INTO retailers (code, owner_name, shop_name, city, phone, language, credit_limit, segment)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"P{1001 + i}", name, name, city or "—", mobile or "", info.get("language") or guess_language(city),
                 info.get("credit_limit") or 0, segment(key)),
            ).lastrowid

        invoice_ids = {}
        for b in bills:
            rid = retailer_ids[_party_key(b["party"])]
            invoice_ids[(rid, b["bill_no"])] = conn.execute(
                "INSERT INTO invoices (number, retailer_id, date, due_date, amount, paid) VALUES (?,?,?,?,?,?)",
                (b["bill_no"], rid, b["bill_date"].isoformat(), b["due_date"].isoformat(), b["amount"],
                 b["amount"] - b["pending"]),
            ).lastrowid

        sku_ids, settled = {}, set()
        for l in lines:
            key = l["item"].lower()
            if key not in sku_ids:
                sku_ids[key] = conn.execute(
                    "INSERT INTO skus (code, name, name_hi, category, unit, price) VALUES (?,?,?,?,?,?)",
                    (f"I{len(sku_ids) + 1:04d}", l["item"], l["item"], "Imported", "unit", l["rate"]),
                ).lastrowid
            rid = retailer_ids[_party_key(l["party"])]
            ref = (rid, l["bill_no"] or f"{l['date'].isoformat()}")
            if ref not in invoice_ids:
                # A sale with no outstanding balance: record it as a settled bill for purchase history.
                invoice_ids[ref] = conn.execute(
                    "INSERT INTO invoices (number, retailer_id, date, due_date, amount, paid) VALUES (?,?,?,?,0,0)",
                    (l["bill_no"] or f"S-{l['date'].isoformat()}", rid, l["date"].isoformat(), l["date"].isoformat()),
                ).lastrowid
                settled.add(invoice_ids[ref])
            conn.execute("INSERT INTO invoice_lines (invoice_id, sku_id, qty, rate) VALUES (?,?,?,?)",
                         (invoice_ids[ref], sku_ids[key], l["qty"], l["rate"]))
        for inv_id in settled:
            conn.execute("""UPDATE invoices SET amount = (SELECT COALESCE(SUM(qty * rate), 0) FROM invoice_lines
                            WHERE invoice_id = ?), paid = (SELECT COALESCE(SUM(qty * rate), 0) FROM invoice_lines
                            WHERE invoice_id = ?) WHERE id = ?""", (inv_id, inv_id, inv_id))
        conn.execute("UPDATE business SET erp = ?, data_source = 'imported', imported_at = ? WHERE id = 1",
                     (erp, datetime.now().isoformat(timespec="seconds")))
    return {"retailers": len(retailer_ids), "bills": len(bills), "sales_lines": len(lines), "items": len(sku_ids),
            "as_of": today.isoformat()}


def discard(token):
    import shutil
    for path in uploaded(token).values():
        shutil.rmtree(path.parent, ignore_errors=True)
        break
