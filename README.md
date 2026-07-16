# Graas costing & proposal calculator (Streamlit)

A costing/proposal builder with a shared tracker, replacing manual rebuilds of the reference
costing spreadsheet. Unlike the Claude artifact version, this is a normal web app: once deployed,
everyone who opens the URL sees the same tracker automatically — no Claude account, no publish/share
permissions, no org membership required.

## Run it locally first

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. Try "Load / refresh historical deals" on the Tracker tab to
confirm the 5 past deals load with the correct stages (Actually Group = Won, the other three =
Cancelled).

## Deploy so management can open it from a link

Same flow as your existing `chat-analyser-bx.streamlit.app`:

1. Push this folder (`app.py`, `requirements.txt`) to a GitHub repo (private is fine).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, click **New app**.
3. Point it at the repo, branch, and `app.py`.
4. Deploy. You get a URL like `graas-costing.streamlit.app` — send that link to anyone; it opens
   in a plain browser, no login of any kind needed.

## Where the data lives

Saved costings sit in `costings.db` (SQLite), created automatically next to `app.py` on first
save. Every visitor to the deployed app reads and writes that same file, so the tracker is shared
by default — there's no private/shared toggle to manage.

**One thing to know about Streamlit Community Cloud specifically:** its filesystem is not
permanent storage — a redeploy (e.g. pushing a code change, or the app going idle and waking back
up on some tiers) can reset it. For anything you need to keep long-term:
- Click **Load / refresh historical deals** any time after a redeploy to restore the 5 reference
  deals (it's idempotent — safe to click repeatedly).
- If the tracker holds real client deals you can't afford to lose, consider swapping the storage
  to a small external database (e.g. Supabase or a hosted Postgres free tier) — say the word and
  I can wire that in instead of SQLite; it's a modest change since all the reads/writes are
  already isolated in the `save_costing` / `load_all_costings` / `delete_costing` functions at the
  top of `app.py`.

## What's in the app

- **New costing** — project details, quick-start templates (Custom API Integration, Vend/POS,
  WMS Warehouse Setup, Data Extract Service), editable rate card with a global discount %, editable
  one-time and monthly line-item tables, a tiered per-order fee calculator (mirrors the Actually
  Group WMS pricing), live summary metrics, and a client-ready quote text generator.
- **Tracker & history** — every saved costing in one shared table, a pipeline total (excluding
  Lost/Cancelled deals), and load/delete controls for any saved record.
