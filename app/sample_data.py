"""Generates a realistic sample book for a staples distributor in Punjab.

Everything here is fake. Retailer phone numbers use the +91 90000 range and are
never dialed: in sample mode every call is redirected to the business's test numbers.
"""
import json
import random
from datetime import date, timedelta

from . import db

FIRST = ["Gurpreet", "Harjeet", "Manpreet", "Rajinder", "Sukhwinder", "Balwinder", "Amarjit",
         "Paramjit", "Kuldeep", "Jaswant", "Ramesh", "Suresh", "Rakesh", "Anil", "Vijay",
         "Sunil", "Deepak", "Mohan", "Satish", "Naresh", "Harpreet", "Simranjit", "Pawan", "Ashok"]
LAST = ["Singh", "Sandhu", "Gill", "Dhillon", "Grewal", "Sidhu", "Bains", "Kumar", "Sharma",
        "Arora", "Gupta", "Bansal", "Goyal", "Mittal", "Jindal", "Verma"]
SHOP = ["Kiryana Store", "General Store", "Provision Store", "Super Mart", "Traders",
        "Departmental Store", "Kirana & Sons", "Mart"]
SHOP_PREFIX = ["Waheguru", "Shiv", "Guru Nanak", "Balaji", "Sai", "Durga", "Jai Mata Di",
               "New", "Royal", "Punjab", "Sat Kartar", "Krishna", "Hanuman", "Laxmi", "Ganpati"]
CITIES = [("Ludhiana", 0.6), ("Jalandhar", 0.65), ("Amritsar", 0.75), ("Patiala", 0.6),
          ("Moga", 0.85), ("Khanna", 0.7), ("Phagwara", 0.65), ("Ambala", 0.2), ("Panipat", 0.05)]

SKUS = [
    ("RIC-25", "Basmati Rice 25 kg", "बासमती चावल 25 किलो", "Rice", "bag", 2150),
    ("RIC-10", "Sella Rice 10 kg", "सेला चावल 10 किलो", "Rice", "bag", 720),
    ("ATA-10", "Chakki Atta 10 kg", "चक्की आटा 10 किलो", "Flour", "bag", 420),
    ("ATA-50", "Chakki Atta 50 kg", "चक्की आटा 50 किलो", "Flour", "bag", 1950),
    ("BES-01", "Besan 1 kg", "बेसन 1 किलो", "Flour", "pack", 95),
    ("OIL-MS15", "Mustard Oil 15 L tin", "सरसों का तेल 15 लीटर टिन", "Oil", "tin", 2280),
    ("OIL-RF15", "Refined Soyabean Oil 15 L", "रिफाइंड सोयाबीन तेल 15 लीटर", "Oil", "tin", 1980),
    ("OIL-MS01", "Mustard Oil 1 L pouch (12)", "सरसों तेल 1 लीटर पाउच, 12 का डिब्बा", "Oil", "carton", 1890),
    ("GHE-15", "Desi Ghee 15 kg tin", "देसी घी 15 किलो टिन", "Ghee", "tin", 8400),
    ("GHE-01", "Desi Ghee 1 L (10)", "देसी घी 1 लीटर, 10 का डिब्बा", "Ghee", "carton", 6200),
    ("SUG-50", "Sugar 50 kg", "चीनी 50 किलो", "Sugar", "bag", 2100),
    ("JAG-10", "Jaggery 10 kg", "गुड़ 10 किलो", "Sugar", "box", 520),
    ("DAL-MS30", "Masoor Dal 30 kg", "मसूर दाल 30 किलो", "Pulses", "bag", 2550),
    ("DAL-CH30", "Chana Dal 30 kg", "चना दाल 30 किलो", "Pulses", "bag", 2400),
    ("DAL-RJ25", "Rajma 25 kg", "राजमा 25 किलो", "Pulses", "bag", 3350),
    ("DAL-UR30", "Urad Dal 30 kg", "उड़द दाल 30 किलो", "Pulses", "bag", 3300),
    ("SPC-HAL", "Haldi Powder 500 g (20)", "हल्दी पाउडर 500 ग्राम, 20 पैकेट", "Spices", "carton", 1700),
    ("SPC-MIR", "Lal Mirch 500 g (20)", "लाल मिर्च 500 ग्राम, 20 पैकेट", "Spices", "carton", 2100),
    ("SPC-GAR", "Garam Masala 100 g (50)", "गरम मसाला 100 ग्राम, 50 पैकेट", "Spices", "carton", 2250),
    ("SAL-25", "Iodised Salt 1 kg (25)", "नमक 1 किलो, 25 पैकेट", "Salt", "bag", 475),
    ("TEA-01", "CTC Tea 1 kg (10)", "चायपत्ती 1 किलो, 10 पैकेट", "Beverages", "carton", 3100),
    ("SOA-72", "Detergent Bar (72)", "कपड़े धोने का साबुन, 72 पीस", "Home care", "carton", 1150),
    ("DET-01", "Detergent Powder 1 kg (20)", "डिटर्जेंट पाउडर 1 किलो, 20 पैकेट", "Home care", "carton", 1850),
    ("BIS-48", "Glucose Biscuits (48)", "ग्लूकोज़ बिस्कुट, 48 पैकेट", "Snacks", "carton", 430),
]

# Baskets a retailer tends to buy from; drives purchase history and recommendations.
BASKETS = {
    "staples": ["ATA-10", "ATA-50", "RIC-10", "SUG-50", "SAL-25", "DAL-CH30", "DAL-MS30", "OIL-MS15"],
    "premium": ["RIC-25", "GHE-01", "GHE-15", "OIL-RF15", "DAL-RJ25", "TEA-01", "SPC-GAR"],
    "kitchen": ["SPC-HAL", "SPC-MIR", "SPC-GAR", "BES-01", "JAG-10", "OIL-MS01", "DAL-UR30"],
    "general": ["SOA-72", "DET-01", "BIS-48", "TEA-01", "SAL-25", "OIL-MS01"],
}
SEGMENTS = {
    "Large": dict(limit=(500000, 900000), orders=(14, 22), lines=(5, 9), qty=(2, 8)),
    "Medium": dict(limit=(200000, 400000), orders=(9, 15), lines=(3, 7), qty=(1, 5)),
    "Small": dict(limit=(60000, 150000), orders=(5, 10), lines=(2, 5), qty=(1, 4)),
}
# How each retailer pays: share of invoice value settled, and the typical delay.
BEHAVIOUR = [("prompt", 0.35, (5, 15)), ("steady", 0.35, (20, 40)),
             ("late", 0.2, (45, 75)), ("chronic", 0.1, (80, 130))]


def generate(business_name, owner_name, owner_mobile, city="Ludhiana", erp="Busy",
             test_numbers=(), retailers=60, seed=None):
    rnd = random.Random(seed)
    today = date.today()
    with db.tx() as conn:
        for table in ("calls", "campaigns", "payments", "invoice_lines", "invoices",
                      "skus", "retailers", "business"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT INTO business (id, name, owner_name, owner_mobile, city, erp, test_numbers)"
            " VALUES (1, ?, ?, ?, ?, ?, ?)",
            (business_name, owner_name, owner_mobile, city, erp, json.dumps(list(test_numbers))),
        )
        sku_ids = {}
        for code, name, name_hi, category, unit, price in SKUS:
            sku_ids[code] = conn.execute(
                "INSERT INTO skus (code, name, name_hi, category, unit, price) VALUES (?,?,?,?,?,?)",
                (code, name, name_hi, category, unit, price),
            ).lastrowid
        price_of = {s[0]: s[5] for s in SKUS}

        used_shops = set()
        invoice_no = 1001
        for i in range(retailers):
            city_name, punjabi_share = rnd.choice(CITIES)
            segment = rnd.choices(list(SEGMENTS), weights=[2, 5, 4])[0]
            spec = SEGMENTS[segment]
            first = rnd.choice(FIRST)
            shop = f"{rnd.choice(SHOP_PREFIX)} {rnd.choice(SHOP)}"
            while shop in used_shops:
                shop = f"{rnd.choice(SHOP_PREFIX)} {rnd.choice(SHOP)}"
            used_shops.add(shop)
            language = "Punjabi" if rnd.random() < punjabi_share else "Hindi"
            limit = round(rnd.uniform(*spec["limit"]), -3)
            rid = conn.execute(
                "INSERT INTO retailers (code, owner_name, shop_name, city, phone, language, credit_limit, segment)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"R{1001 + i}", f"{first} {rnd.choice(LAST)}", shop, city_name,
                 f"+9190000{10000 + i:05d}", language, limit, segment),
            ).lastrowid

            baskets = rnd.sample(list(BASKETS), k=rnd.choice([1, 2, 2, 3]))
            favourites = sorted({c for b in baskets for c in BASKETS[b]})
            _, _, delay = rnd.choices(BEHAVIOUR, weights=[b[1] for b in BEHAVIOUR])[0]

            n_orders = rnd.randint(*spec["orders"])
            order_days = sorted(rnd.sample(range(3, 180), k=n_orders))
            for days_ago in order_days:
                inv_date = today - timedelta(days=days_ago)
                lines = rnd.sample(favourites, k=min(len(favourites), rnd.randint(*spec["lines"])))
                if rnd.random() < 0.15:  # occasional off-basket purchase
                    lines.append(rnd.choice([s[0] for s in SKUS]))
                items = [(code, rnd.randint(*spec["qty"])) for code in dict.fromkeys(lines)]
                amount = sum(price_of[c] * q for c, q in items)
                due = inv_date + timedelta(days=rnd.choice([15, 21, 30]))
                pay_after = rnd.randint(*delay)
                if days_ago >= pay_after:
                    paid = amount if rnd.random() < 0.97 else round(amount * rnd.uniform(0.3, 0.8), -1)
                else:
                    paid = 0
                inv_id = conn.execute(
                    "INSERT INTO invoices (number, retailer_id, date, due_date, amount, paid) VALUES (?,?,?,?,?,?)",
                    (f"INV-{invoice_no}", rid, inv_date.isoformat(), due.isoformat(), amount, paid),
                ).lastrowid
                invoice_no += 1
                for code, qty in items:
                    conn.execute(
                        "INSERT INTO invoice_lines (invoice_id, sku_id, qty, rate) VALUES (?,?,?,?)",
                        (inv_id, sku_ids[code], qty, price_of[code]),
                    )
                if paid:
                    conn.execute(
                        "INSERT INTO payments (retailer_id, date, amount, mode) VALUES (?,?,?,?)",
                        (rid, (inv_date + timedelta(days=pay_after)).isoformat(), paid,
                         rnd.choice(["UPI", "UPI", "Cash", "Cheque", "NEFT"])),
                    )
