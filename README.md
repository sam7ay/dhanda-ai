# Dhanda AI

A dashboard for distributors (MSMEs) who give goods on credit to small retailers. A Sarvam voice agent calls retailers in Hindi or Punjabi to remind them about overdue payments, collects a payment date, suggests stock they usually reorder, and hands the call to the owner when the retailer asks for them.

It starts on **sample data** (60 generated retailers with bills, payments and purchase history). To use real books, go to **Connect your books** and upload Busy or Marg exports: bill-wise outstanding (required), item-wise sales and the party list (both optional). Columns are matched by name, and you see a preview before anything is imported. The page also has example files you can download.

## Run it

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000 for the public homepage. **Try it free** opens a ready-made demo business (set `DHANDA_DEMO=1`). **Set up my business** creates your own. You log in with the owner's mobile and a one-time code. No SMS gateway is connected yet, so the code appears on screen.

Without any Sarvam setup, calls are **simulated**. Everything works end to end so you can demo it, and each simulated call is labelled as such.

## Make real calls through Sarvam

1. **Create the agent.** In the Sarvam dashboard, create an agent using [agent/collections-agent.md](agent/collections-agent.md): the greeting, instructions, input and output variables, Hindi and Punjabi languages, and call forwarding to the `owner_mobile` variable. Commit it.
2. **Get a number.** Deploy → Phone Numbers → Rent from Sarvam (needs KYC). Note the connection ID and the number.
3. **Create a Voice Agents API key.** Settings → API Key.
4. **Fill in `.env`** (see [.env.example](.env.example)): `SARVAM_VA_API_KEY`, `SARVAM_ORG_ID`, `SARVAM_WORKSPACE_ID`, `SARVAM_APP_ID`, `SARVAM_APP_VERSION`, `SARVAM_CONNECTION_ID`, `SARVAM_AGENT_NUMBER`. Restart the server.
5. **Add test numbers** in Settings. These are phones you or your team carry.

After that, **Live via Sarvam** unlocks on campaigns and on "Call now".

**Safety rule:** while the book is sample data, every live call is redirected to a test number. The sample retailers' numbers (+91 90000…) are never dialed.

Call results come back in two ways:
- **Refresh results** pulls them from Sarvam's analytics API. No public URL is needed.
- If you set `PUBLIC_BASE_URL` (for example an ngrok URL), Sarvam posts each result to `/webhooks/sarvam` as soon as the call ends. Each webhook is checked against `DHANDA_WEBHOOK_SECRET`.

## Code map

| File | What it does |
|---|---|
| `app/main.py` | Routes: setup, login, dashboard, retailers, campaigns, calls, settings, webhook |
| `app/ledger.py` | Outstanding, ageing, risk, reorder and cross-sell suggestions, agent variables |
| `app/calling.py` | Launches live (Sarvam) or simulated calls; stores outcomes from webhooks and analytics |
| `app/sarvam_va.py` | Sarvam Voice Agents API: campaigns, cohorts, instant calls, analytics, transcripts |
| `app/sample_data.py` | Generates the sample book |
| `app/erp_import.py` | Reads Busy / Marg exports (xlsx or csv), matches columns, imports the ledger |
| `app/sample_exports.py` | Writes example Busy / Marg exports from the current book |
| `agent/collections-agent.md` | The agent's settings and prompt, to paste into Sarvam |

## Deploying on Vercel

Vercel finds the FastAPI `app` in `app/main.py` automatically, and the demo button is always on there. The database lives in `/tmp`, so it **resets whenever the function restarts**. That's fine for a demo, but move to a hosted database before real customers use it. Add the Sarvam variables in the Vercel project settings, never in the repo.

## Not built yet

- Automatic sync from Busy or Marg. Today you upload exports by hand.
- A hosted database (needed for Vercel or any multi-user deployment).
- SMS for login codes.
- One login per business. Multi-tenant accounts come before onboarding a second client.
