"""Writes the current book out as Busy- and Marg-style report exports, so the importer can be demoed.

The layouts follow the usual shape of these reports (title lines, party heading rows, Dr/Cr amounts),
but real exports vary by version and report settings — that's why the importer maps columns by name.
"""
import csv
import io
from collections import defaultdict
from datetime import date

from openpyxl import Workbook

from . import db

KINDS = {"bills": "Bill-wise outstanding", "sales": "Item-wise sales", "parties": "Party list"}


def _d(iso):
    return date.fromisoformat(iso).strftime("%d-%m-%Y")


def _amt(v):
    return f"{v:,.2f} Dr"


def _data():
    retailers = {r["id"]: r for r in db.rows("SELECT * FROM retailers")}
    bills = db.rows("SELECT * FROM invoices WHERE amount > paid ORDER BY retailer_id, date")
    sales = db.rows("""SELECT i.retailer_id, i.number, i.date, s.name, s.unit, l.qty, l.rate
                       FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id JOIN skus s ON s.id = l.sku_id
                       ORDER BY i.date, i.number""")
    return retailers, bills, sales


def _rows(erp, kind):
    business = db.business()
    retailers, bills, sales = _data()
    today = date.today()
    title = [[business["name"]], [KINDS[kind]], [f"As on {today.strftime('%d-%m-%Y')}"], []]
    if kind == "bills":
        if erp == "Busy":
            rows = title + [["Party Name", "Bill No.", "Bill Date", "Due Date", "Bill Amt.", "Pending Amt.", "Days"]]
            by_party = defaultdict(list)
            for b in bills:
                by_party[b["retailer_id"]].append(b)
            for rid, items in by_party.items():
                rows.append([retailers[rid]["shop_name"]])
                for b in items:
                    rows.append(["", b["number"], _d(b["date"]), _d(b["due_date"]), _amt(b["amount"]),
                                 _amt(b["amount"] - b["paid"]), (today - date.fromisoformat(b["date"])).days])
                rows.append(["Total", "", "", "", "", _amt(sum(b["amount"] - b["paid"] for b in items)), ""])
            return rows
        rows = [["Party Name", "Station", "Bill No", "Bill Date", "Due Date", "Bill Amount", "Balance", "Days"]]
        for b in bills:
            r = retailers[b["retailer_id"]]
            rows.append([r["shop_name"], r["city"], b["number"], _d(b["date"]), _d(b["due_date"]),
                         round(b["amount"], 2), round(b["amount"] - b["paid"], 2), (today - date.fromisoformat(b["date"])).days])
        return rows
    if kind == "sales":
        header = (["Date", "Vch/Bill No.", "Party Name", "Item Name", "Qty.", "Unit", "Price", "Amount"] if erp == "Busy"
                  else ["Bill Date", "Bill No", "Party", "Product", "Qty", "Unit", "Rate", "Value"])
        rows = (title if erp == "Busy" else []) + [header]
        last = None
        for s in sales:
            first = s["number"] != last
            last = s["number"]
            r = retailers[s["retailer_id"]]
            rows.append([_d(s["date"]) if first or erp == "Marg" else "", s["number"] if first or erp == "Marg" else "",
                         r["shop_name"] if first or erp == "Marg" else "", s["name"], s["qty"], s["unit"],
                         round(s["rate"], 2), round(s["qty"] * s["rate"], 2)])
        return rows
    header = (["Name", "City", "Mobile No.", "Credit Limit"] if erp == "Busy"
              else ["Party Name", "Station", "Mobile No", "Cr Limit"])
    rows = (title if erp == "Busy" else []) + [header]
    for r in retailers.values():
        rows.append([r["shop_name"], r["city"], r["phone"][3:] if r["phone"] else "", r["credit_limit"]])
    return rows


def export(erp, kind):
    """Returns (filename, bytes, media type). Busy → .xlsx, Marg → .csv, as each usually exports."""
    rows = _rows(erp, kind)
    stem = f"{erp.lower()}-{kind}"
    if erp == "Busy":
        wb = Workbook()
        ws = wb.active
        ws.title = KINDS[kind][:30]
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return f"{stem}.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return f"{stem}.csv", buf.getvalue().encode("utf-8"), "text/csv"
