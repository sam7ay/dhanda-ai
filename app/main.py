import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent

# Load .env into the environment (same approach as chat.py, no extra dependency)
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

from . import calling, db, erp_import, ledger, sample_data, sample_exports, sarvam_va  # noqa: E402

app = FastAPI(title="Dhanda AI")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")
templates.env.filters["inr"] = ledger.inr
templates.env.globals["OUTCOMES"] = calling.OUTCOMES

SESSION_COOKIE = "dhanda_session"
DEMO_MOBILE = calling.DEMO_MOBILE


def demo_enabled():
    return os.environ.get("DHANDA_DEMO") == "1" or bool(os.environ.get("VERCEL"))


def _fmt_date(value, fmt="%d %b %Y"):
    if not value:
        return "—"
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return value
    if d.tzinfo:
        d = d.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return d.strftime(fmt)


templates.env.filters["date"] = _fmt_date
templates.env.filters["mask"] = lambda p: (p[:3] + " ••••• " + p[-4:]) if p and len(p) > 8 else p
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def go(path, **params):
    """Redirect with query params safely encoded (error messages contain spaces and symbols)."""
    query = urlencode({k: str(v) for k, v in params.items()})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


normalise_mobile = ledger.normalise_mobile


PROTECTED_PREFIXES = ("/dashboard", "/retailers", "/campaigns", "/live", "/api/live", "/calls", "/import",
                      "/agent", "/settings", "/sync")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PROTECTED_PREFIXES):
        return await call_next(request)
    profile = read_profile(request)
    if not profile:
        return RedirectResponse("/login", status_code=303)
    if not profile.get("b"):
        return RedirectResponse("/setup", status_code=303)
    open_workspace(profile)
    return await call_next(request)


# ---------- Sessions ----------
# Each business logs in with its own mobile and gets its own database. The session is a signed
# cookie that also carries the business profile, so any Vercel instance (each with its own empty
# /tmp) can rebuild the same workspace on demand.

def _session_secret():
    secret = os.environ.get("DHANDA_SESSION_SECRET") or os.environ.get("DHANDA_WEBHOOK_SECRET")
    return (secret or "dhanda-" + os.environ.get("VERCEL_PROJECT_ID", str(db.BASE_DIR))).encode()


def _sign(payload):
    return hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def _pack(data):
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    return f"{body}.{_sign(body)}"


def _unpack(token):
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if data.get("x", 0) > time.time() else None


def read_profile(request):
    """{m: mobile, b: business, o: owner, c: city, e: erp, t: [[name, mobile], ...]} or None."""
    return _unpack(request.cookies.get(SESSION_COOKIE, ""))


def with_profile(resp, profile, days=14):
    profile = {**profile, "x": int(time.time()) + 60 * 60 * 24 * days}
    resp.set_cookie(SESSION_COOKIE, _pack(profile), httponly=True, samesite="lax", max_age=60 * 60 * 24 * days)
    return resp


def tenant_for(mobile):
    return "demo" if mobile == DEMO_MOBILE else "u" + hashlib.sha256(mobile.encode()).hexdigest()[:16]


def open_workspace(profile):
    """Point this request at the user's database, building their sample book if it isn't there yet."""
    db.use_tenant(tenant_for(profile["m"]))
    if db.business():
        return
    if profile["m"] == DEMO_MOBILE:
        _ensure_demo()
        return
    contacts = [{"name": n, "mobile": m} for n, m in profile.get("t", [])]
    seed = int(hashlib.sha256(profile["m"].encode()).hexdigest()[:8], 16)
    sample_data.generate(profile["b"], profile["o"], profile["m"], profile.get("c") or "Ludhiana",
                         profile.get("e") or "Busy", contacts, seed=seed)


def live_allowed(mobile):
    """Real calls spend the Sarvam account's credits, so only approved owner numbers may place them."""
    allowed = {normalise_mobile(m) for m in os.environ.get("DHANDA_LIVE_USERS", "").split(",") if m.strip()}
    return mobile in allowed


def render(request, name, **ctx):
    business = db.business()
    allowed = bool(business) and live_allowed(business["owner_mobile"])
    ctx.update(request=request, business=business, sarvam_ready=sarvam_va.configured(), live_allowed=allowed,
               live_ready=sarvam_va.configured() and allowed and bool(business and business["test_numbers"]),
               path=request.url.path)
    return templates.TemplateResponse(request, name, ctx)


# ---------- Login and setup ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "stage": "mobile"})


@app.post("/login/send-otp", response_class=HTMLResponse)
def send_otp(request: Request, mobile: str = Form(...)):
    normalised = normalise_mobile(mobile)
    if not normalised:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "stage": "mobile", "mobile": mobile,
            "error": "Enter a valid 10-digit Indian mobile number."})
    code = f"{secrets.randbelow(10**6):06d}"
    expires = int(time.time()) + 600
    # The code travels signed (hashed) in the form, so verifying works on any server instance.
    challenge = _pack({"m": normalised, "h": _sign(f"otp.{normalised}.{code}"), "x": expires})
    # No SMS gateway is connected yet, so the code is shown on screen.
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "stage": "otp", "mobile": normalised, "challenge": challenge, "demo_code": code})


@app.post("/login/verify")
def verify_otp(request: Request, challenge: str = Form(...), code: str = Form(...)):
    data = _unpack(challenge)
    if not data or not hmac.compare_digest(data["h"], _sign(f"otp.{data['m']}.{code.strip()}")):
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "stage": "otp", "mobile": (data or {}).get("m", ""), "challenge": challenge,
            "error": "That code is wrong or has expired."})
    mobile = data["m"]
    previous = read_profile(request)
    if previous and previous.get("m") == mobile and previous.get("b"):
        return with_profile(RedirectResponse("/dashboard", status_code=303), previous)
    db.use_tenant(tenant_for(mobile))
    b = db.business()
    if b:  # this server still has their workspace
        profile = {"m": mobile, "b": b["name"], "o": b["owner_name"], "c": b["city"], "e": b["erp"],
                   "t": [[c["name"], c["mobile"]] for c in b["test_numbers"]]}
        return with_profile(RedirectResponse("/dashboard", status_code=303), profile)
    return with_profile(RedirectResponse("/setup", status_code=303), {"m": mobile})


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    profile = read_profile(request)
    if not profile:
        return RedirectResponse("/login", status_code=303)
    if profile.get("b"):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"request": request, "error": None, "mobile": profile["m"]})


@app.post("/setup")
def setup(request: Request, business_name: str = Form(...), owner_name: str = Form(...),
          city: str = Form("Ludhiana"), erp: str = Form("Busy")):
    profile = read_profile(request)
    if not profile:
        return RedirectResponse("/login", status_code=303)
    profile.update(b=business_name.strip()[:60], o=owner_name.strip()[:60], c=city.strip()[:40] or "Ludhiana",
                   e=erp if erp in ("Busy", "Marg") else "Busy", t=profile.get("t", []))
    db.use_tenant(tenant_for(profile["m"]))
    seed = int(hashlib.sha256(profile["m"].encode()).hexdigest()[:8], 16)
    sample_data.generate(profile["b"], profile["o"], profile["m"], profile["c"], profile["e"], [], seed=seed)
    return with_profile(RedirectResponse("/dashboard", status_code=303), profile)


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _ensure_demo():
    """The same demo book every time (fixed seed), so every instance shows identical data."""
    if db.business():
        return
    sample_data.generate("Sandhu Traders", "Harpreet Sandhu", DEMO_MOBILE, "Ludhiana", "Busy", [], seed=2026)
    overdue = [r["id"] for r in ledger.retailer_summaries() if r["overdue"] > 0]
    calling.launch_campaign("Reminder round, 1st week", overdue[: len(overdue) // 2], "10:00", "19:00",
                            upsell=True, live=False)


@app.get("/demo")
def demo(request: Request):
    """One click into a ready-made sample business, for showing Dhanda AI to customers."""
    if not demo_enabled():
        return RedirectResponse("/login", status_code=303)
    profile = {"m": DEMO_MOBILE, "b": "Sandhu Traders", "o": "Harpreet Sandhu", "c": "Ludhiana", "e": "Busy", "t": []}
    open_workspace(profile)
    return with_profile(RedirectResponse("/dashboard", status_code=303), profile, days=1)


# ---------- Dashboard ----------

def call_stats(calls):
    contacted = [c for c in calls if c["outcome"] in calling.CONTACTED]
    promised = [c for c in calls if c["outcome"] in ("promised", "partial")]
    return {
        "total": len(calls),
        "contacted": len(contacted),
        "promised_count": len(promised),
        "promised_value": sum(c["promised_amount"] or 0 for c in promised),
        "upsell_yes": sum(1 for c in calls if c["upsell_interest"] == "yes"),
        "upsell_maybe": sum(1 for c in calls if c["upsell_interest"] == "maybe"),
        "transferred": sum(1 for c in calls if c["outcome"] == "transferred"),
        "connect_rate": (len(contacted) / len(calls)) if calls else 0,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    profile = read_profile(request)
    return templates.TemplateResponse(request, "home.html", {
        "request": request, "logged_in": bool(profile and profile.get("b")), "has_business": True,
        "demo": demo_enabled()})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    summaries = ledger.retailer_summaries()
    month_start = date.today().replace(day=1).isoformat()
    collected = db.row("SELECT COALESCE(SUM(amount), 0) AS v FROM payments WHERE date >= ?", (month_start,))["v"]
    calls = db.rows("SELECT * FROM calls")
    recent = db.rows("""SELECT c.*, r.shop_name, r.owner_name, r.city FROM calls c
                        JOIN retailers r ON r.id = c.retailer_id ORDER BY c.id DESC LIMIT 8""")
    upcoming = db.rows("""SELECT c.*, r.shop_name, r.city FROM calls c JOIN retailers r ON r.id = c.retailer_id
                          WHERE c.promised_date IS NOT NULL AND c.promised_date >= ?
                          ORDER BY c.promised_date LIMIT 8""", (date.today().isoformat(),))
    ageing = ledger.ageing()
    top = sorted(summaries, key=lambda r: -r["overdue"])[:8]
    return render(request, "dashboard.html",
                  outstanding=sum(r["outstanding"] for r in summaries),
                  overdue=sum(r["overdue"] for r in summaries),
                  overdue_count=sum(1 for r in summaries if r["overdue"] > 0),
                  retailer_count=len(summaries), collected=collected, ageing=ageing,
                  ageing_max=max(ageing.values()) or 1, top=top, recent=recent, upcoming=upcoming,
                  stats=call_stats(calls), imported=request.query_params.get("imported"),
                  checklist=[
                      ("Connect your Busy or Marg books", "Upload your outstanding report", "/import",
                       db.business()["data_source"] == "imported"),
                      ("Add your phone for test calls", "Hear the assistant call you first", "/settings",
                       bool(db.business()["test_numbers"])),
                      ("Start your first reminder round", "Pick overdue shops and press start", "/campaigns/new",
                       bool(calls)),
                  ])


# ---------- Retailers ----------

@app.get("/retailers", response_class=HTMLResponse)
def retailers(request: Request, q: str = "", risk: str = "", status: str = "", sort: str = "overdue"):
    items = ledger.retailer_summaries()
    if q:
        ql = q.lower()
        items = [r for r in items if ql in f"{r['shop_name']} {r['owner_name']} {r['city']} {r['code']}".lower()]
    if risk:
        items = [r for r in items if r["risk"] == risk]
    if status == "overdue":
        items = [r for r in items if r["overdue"] > 0]
    keys = {"overdue": lambda r: -r["overdue"], "outstanding": lambda r: -r["outstanding"],
            "days": lambda r: -r["max_days_overdue"], "name": lambda r: r["shop_name"]}
    items.sort(key=keys.get(sort, keys["overdue"]))
    return render(request, "retailers.html", retailers=items, q=q, risk=risk, status=status, sort=sort)


@app.get("/retailers/{retailer_id}", response_class=HTMLResponse)
def retailer(request: Request, retailer_id: int):
    summary = next((r for r in ledger.retailer_summaries() if r["id"] == retailer_id), None)
    if not summary:
        return RedirectResponse("/retailers", status_code=303)
    return render(request, "retailer.html", r=summary,
                  invoices=ledger.open_invoices(retailer_id),
                  history=ledger.purchase_history(retailer_id),
                  recs=ledger.recommendations(retailer_id),
                  upsell_ok=ledger.upsell_allowed(summary),
                  payments=db.rows("SELECT * FROM payments WHERE retailer_id = ? ORDER BY date DESC LIMIT 6", (retailer_id,)),
                  calls=db.rows("SELECT * FROM calls WHERE retailer_id = ? ORDER BY id DESC", (retailer_id,)),
                  variables=ledger.agent_variables(summary, db.business()))


@app.post("/retailers/{retailer_id}/call")
def retailer_call(retailer_id: int, live: str = Form("")):
    live = live == "1" and sarvam_va.configured() and live_allowed(db.business()["owner_mobile"])
    try:
        call_id = calling.call_now(retailer_id, live)
    except ValueError as exc:
        return go("/settings", error=exc)
    return RedirectResponse(f"/calls/{call_id}", status_code=303)


# ---------- Campaigns ----------

@app.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request):
    items = db.rows("SELECT * FROM campaigns ORDER BY id DESC")
    for c in items:
        calls = db.rows("SELECT * FROM calls WHERE campaign_id = ?", (c["id"],))
        c["stats"] = call_stats(calls)
        c["done"] = sum(1 for x in calls if x["status"] in ("completed", "failed"))
    return render(request, "campaigns.html", campaigns=items)


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new(request: Request, min_days: int = 1, risk: str = ""):
    items = [r for r in ledger.retailer_summaries() if r["overdue"] > 0 and r["max_days_overdue"] >= min_days]
    if risk:
        items = [r for r in items if r["risk"] == risk]
    items.sort(key=lambda r: -r["overdue"])
    for r in items:
        r["upsell_ok"] = ledger.upsell_allowed(r)
    default_name = f"Payment reminders {date.today().strftime('%d %b')}"
    return render(request, "campaign_new.html", retailers=items, min_days=min_days, risk=risk,
                  default_name=default_name)


@app.post("/campaigns")
async def campaign_create(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("retailer_ids")]
    live = form.get("mode") == "live" and sarvam_va.configured() and live_allowed(db.business()["owner_mobile"])
    try:
        cid = calling.launch_campaign(form.get("name") or "Payment reminders", ids,
                                      form.get("window_start") or "10:00", form.get("window_end") or "19:00",
                                      upsell=form.get("upsell") == "1", live=live,
                                      every_day=form.get("every_day") == "1")
    except ValueError as exc:
        return go("/settings", error=exc)
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign(request: Request, campaign_id: int, synced: str = "", error: str = ""):
    c = db.row("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not c:
        return RedirectResponse("/campaigns", status_code=303)
    calls = db.rows("""SELECT c.*, r.shop_name, r.owner_name, r.city, r.language AS pref_language
                       FROM calls c JOIN retailers r ON r.id = c.retailer_id
                       WHERE c.campaign_id = ? ORDER BY c.id""", (campaign_id,))
    outcome_counts = {}
    for x in calls:
        if x["outcome"]:
            outcome_counts[x["outcome"]] = outcome_counts.get(x["outcome"], 0) + 1
    return render(request, "campaign.html", c=c, calls=calls, stats=call_stats(calls),
                  outcome_counts=outcome_counts, synced=synced, error=error)


@app.post("/campaigns/{campaign_id}/status")
def campaign_status(campaign_id: int, action: str = Form(...)):
    c = db.row("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if c and c["mode"] == "live" and c["sarvam_campaign_id"] and action in ("pause", "resume", "cancel"):
        try:
            resp = sarvam_va.set_campaign_status(c["sarvam_campaign_id"], action)
            db.execute("UPDATE campaigns SET status = ? WHERE id = ?", (resp.get("status", action), campaign_id))
        except sarvam_va.SarvamError as exc:
            return go(f"/campaigns/{campaign_id}", error=exc)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.post("/sync")
def sync(back: str = Form("/campaigns")):
    if not back.startswith("/") or back.startswith("//"):
        back = "/campaigns"
    back = back.split("?")[0]
    try:
        n = calling.sync_live_results() if sarvam_va.configured() else 0
    except sarvam_va.SarvamError as exc:
        return go(back, error=exc)
    return go(back, synced=n)


# ---------- Calls ----------

@app.get("/calls", response_class=HTMLResponse)
def calls(request: Request, outcome: str = ""):
    sql = """SELECT c.*, r.shop_name, r.owner_name, r.city FROM calls c
             JOIN retailers r ON r.id = c.retailer_id"""
    params = ()
    if outcome:
        sql += " WHERE c.outcome = ?"
        params = (outcome,)
    items = db.rows(sql + " ORDER BY c.id DESC LIMIT 300", params)
    return render(request, "calls.html", calls=items, outcome=outcome)


@app.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: int):
    c = db.row("""SELECT c.*, r.shop_name, r.owner_name, r.city, r.language AS pref_language, r.id AS rid
                  FROM calls c JOIN retailers r ON r.id = c.retailer_id WHERE c.id = ?""", (call_id,))
    if not c:
        return RedirectResponse("/calls", status_code=303)
    return render(request, "call.html", c=c, variables=json.loads(c["variables"]),
                  transcript=json.loads(c["transcript"]) if c["transcript"] else [])


# ---------- Settings and agent ----------

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, error: str = "", saved: str = ""):
    return render(request, "settings.html", missing=sarvam_va.missing(), error=error, saved=saved,
                  key_check=sarvam_va.check(),
                  public_url=os.environ.get("PUBLIC_BASE_URL", ""),
                  agent_number=os.environ.get("SARVAM_AGENT_NUMBER", ""))


@app.post("/settings")
def settings_save(request: Request, business_name: str = Form(...), owner_name: str = Form(...),
                  erp: str = Form("Busy"), test_numbers: str = Form("")):
    try:
        tests = ledger.parse_contacts(test_numbers)[:20]
    except ValueError as exc:
        return go("/settings", error=exc)
    erp = erp if erp in ("Busy", "Marg") else "Busy"
    db.execute("UPDATE business SET name = ?, owner_name = ?, erp = ?, test_numbers = ? WHERE id = 1",
               (business_name.strip()[:60], owner_name.strip()[:60], erp, json.dumps(tests)))
    # Keep the cookie profile in step, so other server instances rebuild the same settings.
    profile = {**read_profile(request), "b": business_name.strip()[:60], "o": owner_name.strip()[:60], "e": erp,
               "t": [[c["name"], c["mobile"]] for c in tests]}
    return with_profile(RedirectResponse("/settings?saved=1", status_code=303), profile)


@app.post("/settings/regenerate")
def regenerate():
    b = db.business()
    sample_data.generate(b["name"], b["owner_name"], b["owner_mobile"], b["city"], b["erp"], b["test_numbers"])
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/agent", response_class=HTMLResponse)
def agent(request: Request):
    prompt = (ROOT / "agent" / "collections-agent.md").read_text()
    sample = ledger.retailer_summaries()
    sample = max(sample, key=lambda r: r["overdue"]) if sample else None
    return render(request, "agent.html", prompt=prompt,
                  variables=ledger.agent_variables(sample, db.business()) if sample else {})


# ---------- Live calls ----------

_live_cache = {}


def _live_attempts(owner_mobile):
    """This business's recent attempts straight from Sarvam (cached a few seconds), so the view
    survives server restarts. One Sarvam account serves every business, so filter by owner."""
    cached = _live_cache.get(owner_mobile)
    if cached and time.time() - cached["at"] < 5:
        return cached["items"], cached["error"]
    items, error = [], None
    try:
        calling.sync_live_results()
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        for a in sorted(sarvam_va.list_attempts(since), key=lambda a: a.get("attempted_at") or "", reverse=True):
            v = calling.normalise_vars(a.get("agent_variables"))
            if not owner_mobile or v.get("owner_mobile") != owner_mobile:
                continue
            items.append({
                "when": a.get("attempted_at"), "phone": a.get("user_contact_masked") or "",
                "shop": v.get("shop_name") or "—", "name": v.get("retailer_name") or "",
                "connectivity": a.get("connectivity_status") or "", "duration": a.get("duration_in_seconds") or 0,
                "language": a.get("language_name") or "", "ended_by": a.get("ended_by") or "",
                "outcome": calling.outcome_from(a.get("connectivity_status"), v, a.get("ended_by")),
                "promised_amount": v.get("promised_amount") or "", "promised_date": v.get("promised_date") or "",
                "upsell": v.get("upsell_interest") or "", "summary": v.get("call_summary") or "",
                "interaction_id": a.get("interaction_id") or "", "retry": a.get("next_action_status") or "",
            })
    except sarvam_va.SarvamError as exc:
        error = str(exc)
    _live_cache[owner_mobile] = {"at": time.time(), "items": items, "error": error}
    return items, error


@app.get("/live", response_class=HTMLResponse)
def live(request: Request):
    return render(request, "live.html")


@app.get("/api/live")
def live_api():
    pending = db.rows("""SELECT c.id, c.created_at, c.dialed_number, c.status, r.shop_name FROM calls c
                         JOIN retailers r ON r.id = c.retailer_id
                         WHERE c.mode = 'live' AND c.status IN ('queued', 'scheduled', 'dialing', 'retrying')
                         ORDER BY c.id DESC LIMIT 50""")
    for p in pending:
        p["dialed_number"] = (p["dialed_number"][:3] + "•••••" + p["dialed_number"][-4:]) if p["dialed_number"] else ""
    if not sarvam_va.configured():
        return JSONResponse({"configured": False, "pending": pending, "attempts": [], "error": None})
    owner = db.business()["owner_mobile"]
    attempts, error = _live_attempts(owner if live_allowed(owner) else "")
    return JSONResponse({"configured": True, "pending": pending, "attempts": attempts, "error": error,
                         "outcomes": calling.OUTCOMES})


@app.get("/api/live/transcript")
def live_transcript(interaction_id: str):
    owner = db.business()["owner_mobile"]
    known = _live_cache.get(owner, {}).get("items", [])
    if interaction_id not in {a["interaction_id"] for a in known}:
        return JSONResponse({"messages": [], "error": "Not one of your calls."})
    try:
        return JSONResponse({"messages": sarvam_va.get_transcript(interaction_id)})
    except sarvam_va.SarvamError as exc:
        return JSONResponse({"messages": [], "error": str(exc)})


# ---------- ERP import ----------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, error: str = ""):
    return render(request, "import.html", error=error, kinds=erp_import.REPORT_NAMES)


@app.get("/import/sample/{erp}/{kind}")
def import_sample(erp: str, kind: str):
    if erp not in ("Busy", "Marg") or kind not in sample_exports.KINDS:
        return go("/import")
    name, data, media = sample_exports.export(erp, kind)
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/import/upload")
async def import_upload(request: Request):
    form = await request.form()
    erp = form.get("erp") if form.get("erp") in ("Busy", "Marg") else "Busy"
    token = secrets.token_urlsafe(12)
    saved = 0
    for kind in erp_import.FIELDS:
        f = form.get(kind)
        if isinstance(f, UploadFile) or (hasattr(f, "filename") and f.filename):
            data = await f.read()
            if data:
                erp_import.save_upload(token, kind, f.filename, data)
                saved += 1
    if not saved:
        return go("/import", error="Choose at least the outstanding bills file.")
    return go("/import/review", token=token, erp=erp)


def _review(request, token, erp, overrides=None):
    try:
        report, _ = erp_import.analyse(token, overrides)
    except erp_import.ImportError_ as exc:
        return go("/import", error=exc)
    return render(request, "import_review.html", report=report, erp=erp)


@app.get("/import/review", response_class=HTMLResponse)
def import_review(request: Request, token: str, erp: str = "Busy"):
    return _review(request, token, erp)


def _overrides(form):
    return {k[4:]: v for k, v in form.items() if k.startswith("map:")}


@app.post("/import/review", response_class=HTMLResponse)
async def import_remap(request: Request):
    form = await request.form()
    return _review(request, form.get("token", ""), form.get("erp", "Busy"), _overrides(form))


@app.post("/import/commit")
async def import_commit(request: Request):
    form = await request.form()
    token, erp = form.get("token", ""), form.get("erp", "Busy")
    try:
        report, data = erp_import.analyse(token, _overrides(form))
    except erp_import.ImportError_ as exc:
        return go("/import", error=exc)
    if not data or report["errors"]:
        return _review(request, token, erp, _overrides(form))
    result = erp_import.commit(data, erp)
    erp_import.discard(token)
    return go("/dashboard", imported=result["retailers"])


# ---------- Sarvam webhook ----------

@app.post("/webhooks/sarvam")
async def sarvam_webhook(request: Request):
    payload = await request.json()
    ok = calling.handle_webhook(payload)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 202)
