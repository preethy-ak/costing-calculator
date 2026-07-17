import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import uuid
import math
from datetime import datetime, date

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Graas Costing & Proposal Calculator", layout="wide")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "costings.db")
CCY_SYMBOL = {"USD": "$", "SGD": "S$", "MYR": "RM", "IDR": "Rp", "THB": "฿"}
STAGES = ["Proposed", "Negotiating", "Won", "Lost", "Cancelled"]
PROJECT_TYPES = [
    "Custom API Integration",
    "WMS Warehouse Setup",
    "POS / Vend-style Integration",
    "Data Extract Service",
    "Platform Migration",
    "Other",
]

ONETIME_COLS = ["Description", "Qty/Days", "Unit rate", "Internal cost/unit", "Apply discount", "Remarks"]
MONTHLY_COLS = ["Description", "Qty", "Unit rate / month", "Internal cost/unit", "Apply discount", "Remarks"]

# Internal fully-loaded cost reference, from the "API & Integration Project Pricing" sheet:
# Tech BU weekly cost $2,310 / 6-day week = $385/day; MP BU (PM) weekly cost $2,239 / 6-day week = $373.17/day.
# No distinct internal rate was given for QC/testing, so it defaults to the Tech (developer) rate.
# Final internal & external "Development & Deployment" rate card, per Preethy:
# Internal cost/day — Project Manager $91, Tech Developer $136, Tech Tester+UAT $91 (same as PM).
# External billable/day — Project Manager & Tester/UAT $200 (same rate), Tech Developer $350.
INTERNAL_COST_DEFAULTS = {"pm": 91.0, "dev": 136.0, "qc": 91.0}
EXTERNAL_RATE_DEFAULTS = {"pm": 200.0, "dev": 350.0, "qc": 200.0}


def round_up(x):
    """Company convention: always round costing figures UP, never down or to nearest."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return x
    return math.ceil(x)

# ----------------------------------------------------------------------------
# Storage (SQLite file next to the app — shared automatically by every visitor
# to the deployed instance, since they all hit the same backend file)
# ----------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS costings (
            id TEXT PRIMARY KEY,
            saved_at TEXT,
            date_label TEXT,
            client TEXT,
            project TEXT,
            type TEXT,
            currency TEXT,
            stage TEXT,
            source TEXT,
            discount REAL,
            pm_rate REAL,
            dev_rate REAL,
            qc_rate REAL,
            onetime_json TEXT,
            monthly_json TEXT,
            notes TEXT,
            terms TEXT,
            onetime_total REAL,
            monthly_total REAL
        )"""
    )
    conn.commit()
    # Migration: add columns introduced after initial release, for DBs created before them.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(costings)").fetchall()}
    for col, coltype in [("actual_effort_days", "REAL"), ("final_amount", "REAL")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE costings ADD COLUMN {col} {coltype}")
    conn.commit()
    return conn


def save_costing(record: dict):
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO costings
        (id, saved_at, date_label, client, project, type, currency, stage, source,
         discount, pm_rate, dev_rate, qc_rate, onetime_json, monthly_json, notes,
         terms, onetime_total, monthly_total, actual_effort_days, final_amount)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record["id"], record["saved_at"], record.get("date_label", ""),
            record["client"], record["project"], record["type"], record["currency"],
            record["stage"], record.get("source", ""), record["discount"],
            record["pm_rate"], record["dev_rate"], record["qc_rate"],
            json.dumps(record["onetime_rows"]), json.dumps(record["monthly_rows"]),
            record.get("notes", ""), record.get("terms", ""),
            record["onetime_total"], record["monthly_total"],
            record.get("actual_effort_days"), record.get("final_amount"),
        ),
    )
    conn.commit()
    conn.close()


def update_tracker_fields(cid: str, stage: str, final_amount, actual_effort_days):
    """Used by the Tracker tab's inline editor — updates only stage/final amount/actual effort,
    leaving the rest of the record (line items, notes, etc.) untouched."""
    conn = get_conn()
    conn.execute(
        "UPDATE costings SET stage=?, final_amount=?, actual_effort_days=? WHERE id=?",
        (stage, final_amount, actual_effort_days, cid),
    )
    conn.commit()
    conn.close()


def _row_internal_total(rows):
    """Sum(qty * internal_cost_per_unit), rounded up per line — same convention as compute_line_totals."""
    total = 0
    for r in rows or []:
        qty = r.get("Qty/Days", r.get("Qty", 0)) or 0
        internal = r.get("Internal cost/unit", 0) or 0
        total += round_up(qty * internal) if (qty and internal) else 0
    return total


def _row_effort_days(rows):
    """Sum of Qty/Days across one-time rows — used as the 'quoted effort' figure."""
    total = 0.0
    for r in rows or []:
        total += r.get("Qty/Days", 0) or 0
    return total


def _row_undiscounted_total(rows):
    """Sum(qty * rate) ignoring the Apply-discount checkbox entirely — i.e. what the row would
    bill at full rate-card price, before any negotiated discount. Used to show 'original pricing'
    alongside the actual (possibly discounted) quoted total in the Tracker tab."""
    total = 0
    for r in rows or []:
        qty = r.get("Qty/Days", r.get("Qty", 0)) or 0
        rate = r.get("Unit rate", r.get("Unit rate / month", 0)) or 0
        total += round_up(qty * rate) if (qty and rate) else 0
    return total


def load_all_costings() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM costings", conn)
    conn.close()
    if df.empty:
        return df
    df["yr1_value"] = df["onetime_total"] + df["monthly_total"] * 12
    df["display_date"] = df["date_label"].where(df["date_label"].astype(bool), df["saved_at"])

    onetime_rows_list = df["onetime_json"].apply(lambda s: json.loads(s) if s else [])
    monthly_rows_list = df["monthly_json"].apply(lambda s: json.loads(s) if s else [])
    df["internal_total"] = onetime_rows_list.apply(_row_internal_total) + monthly_rows_list.apply(_row_internal_total)
    df["quoted_effort_days"] = onetime_rows_list.apply(_row_effort_days)
    df["original_pricing"] = onetime_rows_list.apply(_row_undiscounted_total) + monthly_rows_list.apply(_row_undiscounted_total)

    df["quoted_billable"] = df["onetime_total"] + df["monthly_total"]
    df["quoted_margin"] = df["quoted_billable"] - df["internal_total"]
    df["quoted_margin_pct"] = df.apply(
        lambda r: round(r["quoted_margin"] / r["quoted_billable"] * 100, 1) if r["quoted_billable"] else 0.0, axis=1
    )

    # "Final" figures fall back to the quoted ones until Preethy fills in an actual amount/effort.
    df["final_amount_display"] = df["final_amount"].where(df["final_amount"].notna(), df["quoted_billable"])
    df["final_margin"] = df["final_amount_display"] - df["internal_total"]
    df["effort_variance"] = df["quoted_effort_days"] - df["actual_effort_days"]

    df = df.sort_values("saved_at", ascending=False)
    return df


def delete_costing(cid: str):
    conn = get_conn()
    conn.execute("DELETE FROM costings WHERE id=?", (cid,))
    conn.commit()
    conn.close()


def get_costing(cid: str) -> dict:
    conn = get_conn()
    cur = conn.execute("SELECT * FROM costings WHERE id=?", (cid,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    rec = dict(zip(cols, row))
    rec["onetime_rows"] = json.loads(rec["onetime_json"])
    rec["monthly_rows"] = json.loads(rec["monthly_json"])
    return rec


def export_raw_csv() -> bytes:
    """Full-fidelity export (all raw columns, including the line-item JSON) so a restore
    can reconstruct every record exactly — used as a manual backup against the fact that
    Streamlit Community Cloud's free tier does not guarantee this SQLite file survives a
    container restart or redeploy."""
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM costings", conn)
    conn.close()
    return df.to_csv(index=False).encode("utf-8")


def restore_from_csv(file):
    """Restore (upsert) costings from a CSV produced by export_raw_csv(). Returns (ok_count, failed_count)."""
    df = pd.read_csv(file)
    ok, failed = 0, 0
    required = ["id", "saved_at", "client", "project", "type", "currency", "stage",
                "discount", "pm_rate", "dev_rate", "qc_rate", "onetime_json", "monthly_json",
                "onetime_total", "monthly_total"]
    for _, r in df.iterrows():
        try:
            record = {
                "id": str(r["id"]),
                "saved_at": str(r["saved_at"]),
                "date_label": str(r.get("date_label", "") or ""),
                "client": str(r["client"]),
                "project": str(r["project"]),
                "type": str(r["type"]),
                "currency": str(r["currency"]),
                "stage": str(r["stage"]),
                "source": str(r.get("source", "") or ""),
                "discount": float(r["discount"]),
                "pm_rate": float(r["pm_rate"]),
                "dev_rate": float(r["dev_rate"]),
                "qc_rate": float(r["qc_rate"]),
                "onetime_rows": json.loads(r["onetime_json"]),
                "monthly_rows": json.loads(r["monthly_json"]),
                "notes": str(r.get("notes", "") or ""),
                "terms": str(r.get("terms", "") or ""),
                "onetime_total": float(r["onetime_total"]),
                "monthly_total": float(r["monthly_total"]),
            }
            save_costing(record)
            ok += 1
        except Exception as e:
            print("restore row failed:", e)
            failed += 1
    return ok, failed


# ----------------------------------------------------------------------------
# Row-level helpers
# ----------------------------------------------------------------------------
def onetime_row(desc="", qty=1.0, rate=0.0, disc=False, remarks="", internal=0.0):
    return {"Description": desc, "Qty/Days": qty, "Unit rate": rate,
            "Internal cost/unit": internal, "Apply discount": disc, "Remarks": remarks}


def monthly_row(desc="", qty=1.0, rate=0.0, disc=False, remarks="", internal=0.0):
    return {"Description": desc, "Qty": qty, "Unit rate / month": rate,
            "Internal cost/unit": internal, "Apply discount": disc, "Remarks": remarks}


def normalize_df(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """Ensure a loaded/older record has every expected column (e.g. records saved before the
    Internal cost/unit column existed) so nothing breaks on load."""
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            df[c] = False if c == "Apply discount" else ("" if c == "Description" or c == "Remarks" else 0.0)
    return df[cols]


def compute_line_totals(df: pd.DataFrame, qty_col: str, rate_col: str, discount_pct: float) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        df["Line total"] = []
        df["Internal cost total"] = []
        df["Margin"] = []
        return df
    # Coerce explicitly to boolean first — a blank/unset checkbox can come through as NaN,
    # and NaN is truthy in Python, which was silently applying the discount to rows whose
    # checkbox was never actually ticked. fillna(False) closes that gap.
    apply_disc = df["Apply discount"].fillna(False).astype(bool)
    mult = apply_disc.apply(lambda d: (1 - discount_pct / 100.0) if d else 1.0)
    # Company convention: costing figures always round UP (ceiling), never nearest or down.
    df["Line total"] = (df[qty_col].fillna(0) * df[rate_col].fillna(0) * mult).apply(round_up)
    df["Internal cost total"] = (df[qty_col].fillna(0) * df.get("Internal cost/unit", 0).fillna(0)).apply(round_up)
    df["Margin"] = df["Line total"] - df["Internal cost total"]
    return df


def fmt_money(n, ccy="USD"):
    sym = CCY_SYMBOL.get(ccy, "")
    return f"{sym}{n:,.2f}"


# ----------------------------------------------------------------------------
# Templates (patterns pulled from past deal structures)
# ----------------------------------------------------------------------------
def apply_template(kind: str, pm, dev, qc, pm_i=None, dev_i=None, qc_i=None):
    pm_i = INTERNAL_COST_DEFAULTS["pm"] if pm_i is None else pm_i
    dev_i = INTERNAL_COST_DEFAULTS["dev"] if dev_i is None else dev_i
    qc_i = INTERNAL_COST_DEFAULTS["qc"] if qc_i is None else qc_i
    st.session_state.discount = 0.0
    if kind == "api":
        st.session_state.ptype = "Custom API Integration"
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Project Management", 6, pm, False, "Deployed across effort duration", internal=pm_i),
            onetime_row("Tech Development", 8, dev, False, "API build + testing hooks", internal=dev_i),
            onetime_row("Testing & UAT", 3, qc, False, "Based on past PUMA UAT effort", internal=qc_i),
        ])
        st.session_state.monthly_df = pd.DataFrame([], columns=MONTHLY_COLS)
        st.session_state.notes = ""
    elif kind == "vend":
        st.session_state.ptype = "POS / Vend-style Integration"
        st.session_state.discount = 30.0
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Project Management", 4, pm, True, "Preferential rate — 30% off standard card", internal=pm_i),
            onetime_row("Tech Development", 4, dev, True, "Custom order-pulling integration", internal=dev_i),
            onetime_row("Testing & UAT", 2, qc, True, "", internal=qc_i),
        ])
        st.session_state.monthly_df = pd.DataFrame([], columns=MONTHLY_COLS)
        st.session_state.notes = ("Any scope changes or delays caused by external dependencies may "
                                   "result in additional costs. Covers all offline stores at no extra "
                                   "charge for adding future stores.")
    elif kind == "wms":
        st.session_state.ptype = "WMS Warehouse Setup"
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Warehouse setup fee — Warehouse 1", 1, 1800, False, "New warehouse: account, locations, training"),
            onetime_row("Warehouse setup fee — Warehouse 2 (10% off)", 1, 1620, False, ""),
            onetime_row("Warehouse setup fee — Warehouse 3 (10% off)", 1, 1620, False, ""),
        ])
        st.session_state.monthly_df = pd.DataFrame([], columns=MONTHLY_COLS)
        st.session_state.notes = ("Setup fees apply only to new warehouses; an existing warehouse "
                                   "already on Graas WMS carries no new setup fee. Per the final Actually "
                                   "Group agreement (confirmed 29 Dec 2025), monthly maintenance is a "
                                   "single per-order fee across all warehouses, not a flat platform fee — "
                                   "use the tiered per-order fee helper below to add it. No internal-cost "
                                   "basis was given for warehouse setup fees, so Internal cost/unit "
                                   "defaults to 0 here — fill it in if you have it.")
    elif kind == "extract":
        st.session_state.ptype = "Data Extract Service"
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Tech build — extract/API hook", 4, dev, False, "One-time build, up to 3 files/delivery", internal=dev_i),
        ])
        st.session_state.monthly_df = pd.DataFrame([
            monthly_row("Extract — Daily frequency", 1, 250, False, "Per store, single location"),
        ])
    elif kind == "clear":
        st.session_state.onetime_df = pd.DataFrame([], columns=ONETIME_COLS)
        st.session_state.monthly_df = pd.DataFrame([], columns=MONTHLY_COLS)
        st.session_state.client = ""
        st.session_state.project = ""
        st.session_state.notes = ""


# ----------------------------------------------------------------------------
# Historical seed data (from the reference costing file + past email threads)
# ----------------------------------------------------------------------------
def historical_records():
    return [
        dict(
            id="hist_decathlon_api", date_label='Costing file — "Full" tab',
            client="Decathlon", project="Custom API Integration (Connect / Listing / Testing)",
            type="Custom API Integration", currency="USD", stage="Cancelled", discount=0,
            pm_rate=250, dev_rate=350, qc_rate=180,
            onetime_rows=[
                onetime_row("Tech Development", 1, 2000, False,
                             "Connect: 2 days, Listing: 5 days, Testing: 2 days, buffer: 3 days (12 days total)"),
                onetime_row("Project Management", 1, 600, False, ""),
            ],
            monthly_rows=[monthly_row("API Maintenance", 1, 500, False, "Per Graas account / brand")],
            notes='From the reference costing workbook, "Full" tab — final price for Decathlon custom integration project.',
            terms="", onetime_total=2600, monthly_total=500,
            source='Costing file: "Full" tab',
        ),
        dict(
            id="hist_actually_vend", date_label="18 Jan 2026 (email)",
            client="Actually Group", project="Vend / Lightspeed order-pulling integration",
            type="POS / Vend-style Integration", currency="SGD", stage="Won", discount=30,
            pm_rate=250, dev_rate=350, qc_rate=180,
            onetime_rows=[
                onetime_row("Project Management", 6, 225.00, False,
                             "6 man-days @ SGD225/day (= SGD1,350 total, already reflects 30% "
                             "preferential discount off standard card)", internal=121.88),
                onetime_row("Tech Development", 4, 337.50, False,
                             "4 man-days @ SGD337.50/day (= SGD1,350 total) — end-to-end integration & deployment",
                             internal=182.15),
                onetime_row("Testing & UAT", 2, 250.00, False,
                             "2 man-days @ SGD250/day (= SGD500 total)", internal=121.88),
            ],
            monthly_rows=[],
            notes=("Confirmed with Kane Gan (Actually Group) via email — covers all offline stores "
                   "(SG, MY, ID) at no extra charge for adding future stores. 50% prepayment before "
                   "project initiation, 50% on completion. Vend integration billed separately from the "
                   "WMS contract. Internal cost basis: PM $91/day, Tech Developer $136/day, Tech "
                   "Tester+UAT $91/day (USD), across 6/4/2 man-days respectively — USD1,273 total, "
                   "SGD1,705 at the FX used in that internal costing sheet (~1.34). That gives a margin "
                   "of roughly SGD1,495 (~47%) on this deal — note this internal cost basis is specific "
                   "to this project's actual staffing plan, and is NOT the same figure as the generic "
                   "Tech/MP BU rate-card cost ($385/$373 per day) used elsewhere in this app as a default "
                   "— the two shouldn't be mixed."),
            terms="50% prepayment before project initiation, 50% upon project completion",
            onetime_total=3200, monthly_total=0,
            source='Email: "Re: WMS" thread, 18 Jan 2026 — Preethy AK to Kane Gan',
        ),
        dict(
            id="hist_modernlink_bc", date_label='Costing file — "Final - Modern Link - Microsoft" tab',
            client="Modern Link", project="Microsoft Business Central integration (pull orders from Graas)",
            type="Custom API Integration", currency="USD", stage="Cancelled", discount=0,
            pm_rate=250, dev_rate=350, qc_rate=180,
            onetime_rows=[
                onetime_row("Tech Development (incl. UAT)", 1, 15000, False, "SGD 19,100"),
                onetime_row("Project Management", 1, 4900, False, "SGD 6,200"),
            ],
            monthly_rows=[
                monthly_row("Custom API integration support & maintenance", 1, 1200, False,
                             "2nd month onwards / upon start of integration — SGD 1,500"),
                monthly_row("OMS (Turbocharger Execute) subscription", 1, 150, False,
                             "Based on orders < 10,000 — SGD 190"),
            ],
            notes=("USD/SGD shown at FX 1.27. 2026 annual expected revenue on file: USD 36,112.86; "
                   "2027 MRR USD 1,350; 2027 ARR USD 16,200."),
            terms="", onetime_total=15000 + 4900, monthly_total=1200 + 150,
            source='Costing file: "Final - Modern Link - Microsoft" tab',
        ),
        dict(
            id="hist_haleon_dksh", date_label='Costing file — "API" tab',
            client="Haleon (DKSH)", project="Custom API integration + data extract service",
            type="Custom API Integration", currency="USD", stage="Cancelled", discount=0,
            pm_rate=250, dev_rate=350, qc_rate=180,
            onetime_rows=[
                onetime_row("Tech Development", 1, 8000, False,
                             "Effort with buffer: ~22 dev days + ~11 UAT days (DKSH ran a 3-week UAT vs. PUMA's 2-week baseline)"),
                onetime_row("Project Management", 1, 2400, False, ""),
            ],
            monthly_rows=[
                monthly_row("Extract service — Daily frequency", 1, 250, False,
                             "Up to 3 files/delivery, per store, single location"),
                monthly_row("Monthly Maintenance", 1, 250, False, "Per platform"),
            ],
            notes=("Pricing based on the rate card shared with DKSH, carried over from the PUMA "
                   "contract structure. Weekly extract alternative: USD150/mo; monthly extract: USD75/mo."),
            terms="", onetime_total=8000 + 2400, monthly_total=250 + 250,
            source='Costing file: "API" tab (Haleon / DKSH)',
        ),
    ]


def seed_historical():
    added, updated = 0, 0
    for rec in historical_records():
        existing = get_costing(rec["id"])
        full = dict(rec)
        full["saved_at"] = existing["saved_at"] if existing else datetime(2000, 1, 1).isoformat()
        save_costing(full)
        updated += 1 if existing else 0
        added += 0 if existing else 1
    return added, updated


# ----------------------------------------------------------------------------
# Session state defaults
# ----------------------------------------------------------------------------
defaults = {
    "client": "", "project": "", "ptype": "Custom API Integration",
    "currency": "USD", "currency2": "—", "fx": 1.35, "stage": "Proposed", "source": "",
    "pm_rate": EXTERNAL_RATE_DEFAULTS["pm"], "dev_rate": EXTERNAL_RATE_DEFAULTS["dev"],
    "qc_rate": EXTERNAL_RATE_DEFAULTS["qc"], "discount": 0.0,
    "pm_internal": INTERNAL_COST_DEFAULTS["pm"], "dev_internal": INTERNAL_COST_DEFAULTS["dev"],
    "qc_internal": INTERNAL_COST_DEFAULTS["qc"],
    "terms": "50% prepayment before start, 50% on completion", "notes": "",
    "onetime_df": pd.DataFrame([
        onetime_row("Project Management", 6, 250, False, "Deployed across effort duration", internal=INTERNAL_COST_DEFAULTS["pm"]),
        onetime_row("Tech Development", 8, 350, False, "API build + testing hooks", internal=INTERNAL_COST_DEFAULTS["dev"]),
        onetime_row("Testing & UAT", 3, 180, False, "Based on past PUMA UAT effort", internal=INTERNAL_COST_DEFAULTS["qc"]),
    ]),
    "monthly_df": pd.DataFrame([], columns=MONTHLY_COLS),
    "tier_orders": 2000.0, "tier_threshold": 2500.0, "tier_rate1": 1.95, "tier_rate2": 1.30,
    "quote_text": "", "recipient_name": "",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ----------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------
st.title("Graas costing & proposal calculator")
st.caption(
    "Build a client-ready costing from rate cards and templates instead of rebuilding the "
    "spreadsheet each time. Everything saved here lives in one shared file on the server — "
    "anyone who opens this app's URL sees the same tracker, no publish/share permissions involved."
)

tab_build, tab_tracker = st.tabs(["New costing", "Tracker & history"])

# ============================================================================
# TAB: NEW COSTING
# ============================================================================
with tab_build:
    col_main, col_summary = st.columns([2, 1], gap="large")

    with col_main:
        st.subheader("Project details")
        c1, c2, c3 = st.columns(3)
        st.session_state.client = c1.text_input("Client", st.session_state.client)
        st.session_state.project = c2.text_input("Project name", st.session_state.project)
        st.session_state.ptype = c3.selectbox("Project type", PROJECT_TYPES,
                                               index=PROJECT_TYPES.index(st.session_state.ptype))

        c4, c5, c6, c7 = st.columns(4)
        st.session_state.currency = c4.selectbox("Currency", list(CCY_SYMBOL.keys()),
                                                  index=list(CCY_SYMBOL.keys()).index(st.session_state.currency))
        ccy2_opts = ["—"] + list(CCY_SYMBOL.keys())
        st.session_state.currency2 = c5.selectbox("Show also in", ccy2_opts,
                                                   index=ccy2_opts.index(st.session_state.currency2))
        st.session_state.fx = c6.number_input("FX rate (1 primary = x secondary)", value=float(st.session_state.fx), step=0.01)
        st.session_state.stage = c7.selectbox("Deal stage", STAGES, index=STAGES.index(st.session_state.stage))
        st.session_state.source = st.text_input("Source / reference (e.g. email thread, costing file tab)", st.session_state.source)

        st.divider()
        st.subheader("Quick-start templates")
        st.caption("Pulled from past deal patterns (DKSH/PUMA rate card, Actually Vend integration, "
                   "WMS warehouse fees, extract services). Loads typical line items — edit anything after.")
        b1, b2, b3, b4, b5 = st.columns(5)
        if b1.button("Custom API Integration", use_container_width=True):
            apply_template("api", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate, st.session_state.pm_internal, st.session_state.dev_internal, st.session_state.qc_internal)
            st.rerun()
        if b2.button("Vend / POS Integration", use_container_width=True):
            apply_template("vend", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate, st.session_state.pm_internal, st.session_state.dev_internal, st.session_state.qc_internal)
            st.rerun()
        if b3.button("WMS Warehouse Setup", use_container_width=True):
            apply_template("wms", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate, st.session_state.pm_internal, st.session_state.dev_internal, st.session_state.qc_internal)
            st.rerun()
        if b4.button("Data Extract Service", use_container_width=True):
            apply_template("extract", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate, st.session_state.pm_internal, st.session_state.dev_internal, st.session_state.qc_internal)
            st.rerun()
        if b5.button("Clear form", use_container_width=True):
            apply_template("clear", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate, st.session_state.pm_internal, st.session_state.dev_internal, st.session_state.qc_internal)
            st.rerun()

        st.divider()
        st.subheader("Rate card & discount")
        st.caption("Final Development & Deployment rate card: Project Manager and Tester/UAT are billed "
                   "at the same rate, $200/day; Tech Developer is $350/day. **This discount % is applied "
                   "per row, not to the overall total** — it only affects rows where you've ticked "
                   "'Apply discount' in the table below; unticked rows are billed at the full rate you "
                   "entered, regardless of this percentage (e.g. Actually Vend integration ticked all "
                   "three rows for a 30% off-card rate). All computed totals round UP, never to nearest "
                   "or down — that's a fixed company convention, not just a display setting.")
        r1, r2, r3, r4 = st.columns(4)
        st.session_state.pm_rate = r1.number_input("PM rate / day", value=float(st.session_state.pm_rate))
        st.session_state.dev_rate = r2.number_input("Developer rate / day", value=float(st.session_state.dev_rate))
        st.session_state.qc_rate = r3.number_input("Tester/QC rate / day (same as PM by default)", value=float(st.session_state.qc_rate))
        st.session_state.discount = r4.number_input("Rate card discount %", value=float(st.session_state.discount))

        with st.expander("Internal cost card (for margin visibility)", expanded=False):
            st.caption(
                "Final internal cost/day: Project Manager $91, Tech Developer $136, Tech Tester+UAT $91 "
                "(same as PM). This replaces the earlier BU-average reference figures — those measured "
                "something different (broad department overhead) and shouldn't be used for per-deal "
                "margin. Edit these only if you have a more specific number for a particular deal."
            )
            ic1, ic2, ic3 = st.columns(3)
            st.session_state.pm_internal = ic1.number_input("PM internal cost / day", value=float(st.session_state.pm_internal))
            st.session_state.dev_internal = ic2.number_input("Developer internal cost / day", value=float(st.session_state.dev_internal))
            st.session_state.qc_internal = ic3.number_input("Tester/QC internal cost / day (same as PM by default)", value=float(st.session_state.qc_internal))

        st.divider()
        st.subheader("One-time costs")
        st.caption('Development effort, setup fees, or any one-off line item. "Internal cost/unit" is '
                   'optional — fill it in to see cost and margin alongside the billable price, right in '
                   'this same table. Tick "Apply discount" to apply the rate-card discount above to that '
                   'row. The **Total** row at the bottom is computed, not editable — anything you type '
                   'into it is discarded. Note: computed columns (including Total) refresh one edit '
                   'behind — they catch up as soon as you commit your next change.')
        onetime_prev = compute_line_totals(
            normalize_df(st.session_state.onetime_df, ONETIME_COLS), "Qty/Days", "Unit rate", st.session_state.discount
        )
        onetime_prev_total = onetime_prev["Line total"].sum() if not onetime_prev.empty else 0.0
        onetime_prev_internal = onetime_prev["Internal cost total"].sum() if not onetime_prev.empty else 0.0
        onetime_merged_in = onetime_prev.rename(columns={"Line total": "Billable", "Internal cost total": "Internal cost"})
        onetime_total_row = pd.DataFrame([{
            "Description": "Total", "Qty/Days": float("nan"), "Unit rate": float("nan"),
            "Internal cost/unit": float("nan"), "Apply discount": False, "Remarks": "",
            "Billable": onetime_prev_total, "Internal cost": onetime_prev_internal,
            "Margin": onetime_prev_total - onetime_prev_internal,
        }])
        onetime_col_order = ["Description", "Qty/Days", "Unit rate", "Internal cost/unit",
                              "Billable", "Internal cost", "Margin", "Apply discount", "Remarks"]
        onetime_editor_input = pd.concat([onetime_merged_in, onetime_total_row], ignore_index=True)[onetime_col_order]
        onetime_edited_full = st.data_editor(
            onetime_editor_input, num_rows="dynamic", use_container_width=True,
            key="onetime_editor",
            disabled=["Billable", "Internal cost", "Margin"],
            column_config={
                "Qty/Days": st.column_config.NumberColumn(format="%.2f"),
                "Unit rate": st.column_config.NumberColumn(format="%.2f"),
                "Internal cost/unit": st.column_config.NumberColumn(format="%.2f"),
                "Apply discount": st.column_config.CheckboxColumn(),
                "Billable": st.column_config.NumberColumn(format="%.2f"),
                "Internal cost": st.column_config.NumberColumn(format="%.2f"),
                "Margin": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        # Drop the synthetic Total row (and anything a user typed into it) before treating the
        # rest as real line items — it's rebuilt fresh from real data on every rerun.
        onetime_real_rows = onetime_edited_full[onetime_edited_full["Description"] != "Total"]
        onetime_edited = onetime_real_rows[ONETIME_COLS]
        st.session_state.onetime_df = onetime_edited
        onetime_calc = compute_line_totals(onetime_edited, "Qty/Days", "Unit rate", st.session_state.discount)
        onetime_total = onetime_calc["Line total"].sum() if not onetime_calc.empty else 0.0
        onetime_internal_total = onetime_calc["Internal cost total"].sum() if not onetime_calc.empty else 0.0

        b_t1, b_t2 = st.columns(2)
        st.session_state.terms = st.text_input("Payment terms", st.session_state.terms)

        st.divider()
        st.subheader("Monthly recurring fees")
        st.caption("Maintenance, subscriptions, per-order fees. These roll into the annualized "
                   "contract value on the right. Internal cost/unit is optional, same as above.")
        monthly_prev = compute_line_totals(
            normalize_df(st.session_state.monthly_df, MONTHLY_COLS), "Qty", "Unit rate / month", st.session_state.discount
        )
        monthly_prev_total = monthly_prev["Line total"].sum() if not monthly_prev.empty else 0.0
        monthly_prev_internal = monthly_prev["Internal cost total"].sum() if not monthly_prev.empty else 0.0
        monthly_merged_in = monthly_prev.rename(columns={"Line total": "Billable / mo", "Internal cost total": "Internal cost / mo"})
        monthly_total_row = pd.DataFrame([{
            "Description": "Total", "Qty": float("nan"), "Unit rate / month": float("nan"),
            "Internal cost/unit": float("nan"), "Apply discount": False, "Remarks": "",
            "Billable / mo": monthly_prev_total, "Internal cost / mo": monthly_prev_internal,
            "Margin": monthly_prev_total - monthly_prev_internal,
        }])
        monthly_col_order = ["Description", "Qty", "Unit rate / month", "Internal cost/unit",
                              "Billable / mo", "Internal cost / mo", "Margin", "Apply discount", "Remarks"]
        monthly_editor_input = pd.concat([monthly_merged_in, monthly_total_row], ignore_index=True)[monthly_col_order]
        monthly_edited_full = st.data_editor(
            monthly_editor_input, num_rows="dynamic", use_container_width=True,
            key="monthly_editor",
            disabled=["Billable / mo", "Internal cost / mo", "Margin"],
            column_config={
                "Qty": st.column_config.NumberColumn(format="%.2f"),
                "Unit rate / month": st.column_config.NumberColumn(format="%.2f"),
                "Internal cost/unit": st.column_config.NumberColumn(format="%.2f"),
                "Apply discount": st.column_config.CheckboxColumn(),
                "Billable / mo": st.column_config.NumberColumn(format="%.2f"),
                "Internal cost / mo": st.column_config.NumberColumn(format="%.2f"),
                "Margin": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        monthly_real_rows = monthly_edited_full[monthly_edited_full["Description"] != "Total"]
        monthly_edited = monthly_real_rows[MONTHLY_COLS]
        st.session_state.monthly_df = monthly_edited
        monthly_calc = compute_line_totals(monthly_edited, "Qty", "Unit rate / month", st.session_state.discount)
        monthly_total = monthly_calc["Line total"].sum() if not monthly_calc.empty else 0.0
        monthly_internal_total = monthly_calc["Internal cost total"].sum() if not monthly_calc.empty else 0.0
        oa1, oa2, oa3 = st.columns(3)
        if oa1.button("+ API Maintenance ($500/mo)", use_container_width=True):
            new_row = pd.DataFrame([monthly_row("API Maintenance", 1, 500, False, "Per Graas account / brand")])
            st.session_state.monthly_df = pd.concat([st.session_state.monthly_df, new_row], ignore_index=True)
            st.rerun()
        if oa2.button("+ OMS/Turbocharger subscription ($150/mo)", use_container_width=True):
            new_row = pd.DataFrame([monthly_row("OMS (Turbocharger Execute) subscription", 1, 150, False, "Based on orders < 10,000")])
            st.session_state.monthly_df = pd.concat([st.session_state.monthly_df, new_row], ignore_index=True)
            st.rerun()
        if oa3.button("+ Agentic Data Analyst ($250/mo, optional)", use_container_width=True):
            new_row = pd.DataFrame([monthly_row("Agentic Data Analyst (hoppr) subscription", 1, 250, False, "Orders < 10,000 & more than 10 platforms")])
            st.session_state.monthly_df = pd.concat([st.session_state.monthly_df, new_row], ignore_index=True)
            st.rerun()

        with st.expander("Tiered per-order fee helper", expanded=False):
            st.caption("Mirrors the WMS-style pricing used with Actually Group (e.g. 2,500 orders @ "
                       "tier-1 rate + overflow @ tier-2 rate). Compute it here, then add as a monthly line item.")
            t1, t2, t3, t4 = st.columns(4)
            st.session_state.tier_orders = t1.number_input("Monthly order volume", value=float(st.session_state.tier_orders))
            st.session_state.tier_threshold = t2.number_input("Tier-1 threshold (orders)", value=float(st.session_state.tier_threshold))
            st.session_state.tier_rate1 = t3.number_input("Tier-1 rate / order", value=float(st.session_state.tier_rate1), step=0.01)
            st.session_state.tier_rate2 = t4.number_input("Tier-2 rate / order (overflow)", value=float(st.session_state.tier_rate2), step=0.01)
            orders, threshold = st.session_state.tier_orders, st.session_state.tier_threshold
            r1v, r2v = st.session_state.tier_rate1, st.session_state.tier_rate2
            if orders <= threshold:
                tier_fee = orders * r1v
                tier_remark = f"{orders:.0f} orders @ {r1v}"
            else:
                tier_fee = threshold * r1v + (orders - threshold) * r2v
                tier_remark = f"{threshold:.0f} @ {r1v} + {orders - threshold:.0f} @ {r2v}"
            st.info(f"Computed fee: **{fmt_money(tier_fee, st.session_state.currency)} / month** for {orders:.0f} orders")
            if st.button("Add computed fee to monthly table"):
                new_row = pd.DataFrame([monthly_row("Per-order fulfilment fee", 1, tier_fee, False, tier_remark)])
                st.session_state.monthly_df = pd.concat([st.session_state.monthly_df, new_row], ignore_index=True)
                st.rerun()

        st.divider()
        st.session_state.notes = st.text_area("Notes / assumptions", st.session_state.notes, height=100)

    # -------------------------------------------------------------------
    # Summary sidebar
    # -------------------------------------------------------------------
    with col_summary:
        st.subheader("Live summary")
        ccy = st.session_state.currency
        annual = onetime_total + monthly_total * 12
        m1, m2 = st.columns(2)
        m1.metric("One-time total (billable)", fmt_money(onetime_total, ccy))
        m2.metric("Monthly recurring (billable)", fmt_money(monthly_total, ccy) + " /mo")
        m1.metric("Year-1 contract value", fmt_money(annual, ccy))
        m2.metric("Total (one-time + monthly)", fmt_money(onetime_total + monthly_total, ccy))

        if st.session_state.currency2 != "—":
            sec_val = (onetime_total + monthly_total) * st.session_state.fx
            st.caption(f"≈ {CCY_SYMBOL.get(st.session_state.currency2,'')}{sec_val:,.2f} in {st.session_state.currency2}")

        st.progress(min(st.session_state.discount / 100.0, 1.0),
                    text=f"{st.session_state.discount:.0f}% rate-card discount applied")

        total_internal = onetime_internal_total + monthly_internal_total
        total_billable = onetime_total + monthly_total
        if total_internal > 0:
            st.divider()
            st.caption("Cost & margin (only counts rows where you've filled in Internal cost/unit)")
            cm1, cm2 = st.columns(2)
            cm1.metric("Internal cost (one-time)", fmt_money(onetime_internal_total, ccy))
            cm2.metric("Internal cost (monthly)", fmt_money(monthly_internal_total, ccy) + " /mo")
            margin_amt = total_billable - total_internal
            margin_pct = (margin_amt / total_billable * 100) if total_billable else 0.0
            cm1.metric("Margin", fmt_money(margin_amt, ccy))
            cm2.metric("Margin %", f"{margin_pct:.1f}%")
            if margin_amt < 0:
                st.warning("Billable total is below internal cost on the rows you've priced — "
                           "worth a second look before sending this out.")

        st.divider()
        if st.button("Save to tracker", type="primary", use_container_width=True):
            record = {
                "id": "costing_" + uuid.uuid4().hex[:12],
                "saved_at": datetime.now().isoformat(),
                "date_label": "",
                "client": st.session_state.client or "Unnamed client",
                "project": st.session_state.project or "Unnamed project",
                "type": st.session_state.ptype,
                "currency": st.session_state.currency,
                "stage": st.session_state.stage,
                "source": st.session_state.source,
                "discount": st.session_state.discount,
                "pm_rate": st.session_state.pm_rate,
                "dev_rate": st.session_state.dev_rate,
                "qc_rate": st.session_state.qc_rate,
                "onetime_rows": onetime_edited.to_dict("records"),
                "monthly_rows": monthly_edited.to_dict("records"),
                "notes": st.session_state.notes,
                "terms": st.session_state.terms,
                "onetime_total": float(onetime_total),
                "monthly_total": float(monthly_total),
            }
            save_costing(record)
            st.success("Saved to the shared tracker.")

        st.session_state.recipient_name = st.text_input(
            "Client contact name (for the email greeting)", st.session_state.recipient_name
        )
        if st.button("✉️ Generate client email draft", use_container_width=True):
            sym = CCY_SYMBOL.get(ccy, "")
            recipient = st.session_state.recipient_name.strip() or "there"
            project = st.session_state.project or "[Project]"
            client_name = st.session_state.client or "[Client]"
            ptype = st.session_state.ptype

            lines = [f"Hi {recipient},", ""]
            lines.append(
                f"Please find below the details of the additional solution you requested for "
                f"{project}, including the scope of work and cost details."
            )
            if ptype in ("Custom API Integration", "POS / Vend-style Integration"):
                lines.append("")
                lines.append(
                    "As this functionality is currently not available in TC, a custom integration "
                    "is required. Accordingly, a one-time integration cost will apply, as discussed "
                    "earlier."
                )
            lines.append("")

            # --- ONE-TIME (billable only — Internal cost/Margin never enter this draft) ---
            if not onetime_calc.empty and onetime_total > 0:
                lines.append(f"One-time integration cost: {sym}{onetime_total:,.2f}")
                quoted_effort = onetime_edited["Qty/Days"].sum() if not onetime_edited.empty else 0
                if quoted_effort:
                    lines.append(f"Estimated effort: {quoted_effort:.0f} working days (development and testing)")
                lines.append("")
                lines.append(f"{client_name}_ {project}")
                lines.append("Scope\tPrice")
                for _, rr in onetime_calc.iterrows():
                    lines.append(f"{rr['Description']}\t{sym}{rr['Line total']:,.2f}")
                lines.append(f"Total Cost\t{sym}{onetime_total:,.2f}")
                lines.append("")

            # --- MONTHLY (billable only) ---
            if not monthly_calc.empty and monthly_total > 0:
                lines.append(
                    f"There will also be a monthly maintenance fee of {sym}{monthly_total:,.2f}, "
                    f"applicable after integration is completed (second month onwards)."
                )
                lines.append("")
                lines.append("Scope\tPrice / month")
                for _, rr in monthly_calc.iterrows():
                    lines.append(f"{rr['Description']}\t{sym}{rr['Line total']:,.2f}")
                lines.append(f"Total\t{sym}{monthly_total:,.2f} / month")
                lines.append("")

            if st.session_state.discount > 0:
                lines.append(
                    f"The above rate reflects a preferential rate discounted {st.session_state.discount:.0f}% "
                    f"from the Graas standard tech rate card."
                )
            lines.append(
                "Please note that any scope changes or delays caused by external dependencies may "
                "result in additional costs."
            )
            lines.append("")
            if st.session_state.terms:
                lines.append(f"Payment terms: {st.session_state.terms}.")
                lines.append("")
            if st.session_state.notes:
                lines.append(st.session_state.notes)
                lines.append("")
            lines.append("Please confirm if this works for you, and we'll proceed accordingly.")
            lines.append("")
            lines.append("Regards,")
            lines.append("PREETHY AK")
            lines.append("Director- Marketplace Delivery")
            lines.append("e. preethy@graas.ai")

            st.session_state.quote_text = "\n".join(lines)

        if st.session_state.quote_text:
            st.text_area("Client-ready email draft", st.session_state.quote_text, height=340)
            st.caption(
                "Select all and copy into your email client. This draft is built only from the "
                "billable ('Unit rate' × qty, after any ticked discount) figures — Internal cost/unit "
                "and Margin are never read when generating this text, so they can't leak into a client "
                "email even by accident."
            )

# ============================================================================
# TAB: TRACKER & HISTORY
# ============================================================================
with tab_tracker:
    top1, top2 = st.columns([3, 1])
    with top1:
        st.subheader("Saved costings")
        st.caption('Every costing you save becomes a record here, shared with anyone who opens this '
                   'app\'s URL. "Load / refresh historical deals" imports the five past deals found in '
                   "the costing file and email threads — safe to click again any time a status changes, "
                   "since it updates those records in place instead of duplicating them.")
    with top2:
        if st.button("Load / refresh historical deals", use_container_width=True):
            added, updated = seed_historical()
            st.success(f"{added} added, {updated} refreshed")
            st.rerun()

    with st.expander("⚠️ Back up / restore (read before you close this tab)", expanded=False):
        st.warning(
            "On Streamlit Community Cloud's free tier, this app's storage is **not guaranteed to "
            "survive a restart** — if the app goes idle and sleeps, or you push a code update, the "
            "tracker can come back empty except for the historical deals. Download a backup before "
            "closing if you've added anything you don't want to lose, and restore it here if the "
            "tracker ever comes back empty."
        )
        bc1, bc2 = st.columns(2)
        with bc1:
            st.download_button(
                "Download all costings (CSV backup)",
                data=export_raw_csv(),
                file_name=f"graas_costings_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with bc2:
            uploaded = st.file_uploader("Restore from a backup CSV", type="csv", key="restore_csv")
            if uploaded is not None:
                ok, failed = restore_from_csv(uploaded)
                st.success(f"Restored {ok} record(s)" + (f", {failed} failed" if failed else ""))
                st.rerun()

    df = load_all_costings()

    if df.empty:
        st.info('No costings saved yet. Build one in "New costing" and click "Save to tracker" — or '
                'click "Load / refresh historical deals" above to import past deals.')
    else:
        pipeline = df[~df["stage"].isin(["Lost", "Cancelled"])]["yr1_value"].sum()
        st.metric("Pipeline (Yr-1 value, excluding Lost/Cancelled)", fmt_money(pipeline, "USD"))

        st.caption(
            "**Original pricing** is what these line items would bill at full rate-card price with no "
            "discount applied — compare it against **Quoted billable** to see how much the **Discount %** "
            "actually took off. Editable right here: **Stage**, **Final confirmed amount** (defaults to "
            "the quoted billable total until you override it), and **Actual effort (days)** — fill that "
            "in once a project wraps up to compare against what was quoted. Everything else is read-only "
            "(edit the underlying costing via 'Load into New costing form' below if it needs to change)."
        )

        edit_cols = ["id", "display_date", "client", "project", "type",
                     "original_pricing", "discount", "quoted_billable", "internal_total",
                     "quoted_margin", "quoted_margin_pct",
                     "quoted_effort_days", "actual_effort_days", "effort_variance",
                     "final_amount_display", "final_margin", "stage", "currency", "source"]
        edit_df = df[edit_cols].rename(columns={
            "display_date": "Date", "client": "Client", "project": "Project", "type": "Type",
            "original_pricing": "Original pricing (full card)", "discount": "Discount %",
            "quoted_billable": "Quoted billable", "internal_total": "Internal cost",
            "quoted_margin": "Quoted margin", "quoted_margin_pct": "Quoted margin %",
            "quoted_effort_days": "Quoted effort (days)", "actual_effort_days": "Actual effort (days)",
            "effort_variance": "Effort variance (days)", "final_amount_display": "Final confirmed amount",
            "final_margin": "Final margin", "stage": "Stage", "currency": "Ccy", "source": "Source",
        })

        edited = st.data_editor(
            edit_df, use_container_width=True, hide_index=True, key="tracker_editor",
            disabled=["id", "Date", "Client", "Project", "Type", "Original pricing (full card)",
                      "Discount %", "Quoted billable", "Internal cost",
                      "Quoted margin", "Quoted margin %", "Quoted effort (days)",
                      "Effort variance (days)", "Final margin", "Ccy", "Source"],
            column_config={
                "id": None,  # hide the raw id column but keep it in the dataframe for saving
                "Stage": st.column_config.SelectboxColumn(options=STAGES),
                "Actual effort (days)": st.column_config.NumberColumn(format="%.2f"),
                "Final confirmed amount": st.column_config.NumberColumn(format="%.2f"),
            },
        )

        if st.button("💾 Save changes to Stage / Final amount / Actual effort", type="primary"):
            n = 0
            for _, r in edited.iterrows():
                orig = df[df["id"] == r["id"]].iloc[0]
                stage_changed = r["Stage"] != orig["stage"]
                final_changed = r["Final confirmed amount"] != orig["final_amount_display"]
                actual_changed = (r["Actual effort (days)"] if pd.notna(r["Actual effort (days)"]) else None) != \
                                  (orig["actual_effort_days"] if pd.notna(orig["actual_effort_days"]) else None)
                if stage_changed or final_changed or actual_changed:
                    update_tracker_fields(
                        r["id"], r["Stage"],
                        None if pd.isna(r["Final confirmed amount"]) else float(r["Final confirmed amount"]),
                        None if pd.isna(r["Actual effort (days)"]) else float(r["Actual effort (days)"]),
                    )
                    n += 1
            if n:
                st.success(f"Updated {n} record(s).")
                st.rerun()
            else:
                st.info("No changes to save.")

        overs = df[df["effort_variance"].notna() & (df["effort_variance"] < 0)]
        if not overs.empty:
            st.warning(
                "These deals took **more** effort than quoted (negative variance = over-effort vs. "
                "the quote): " + ", ".join(f"{r.client} ({r.effort_variance:+.1f}d)" for r in overs.itertuples())
            )

        st.divider()
        st.subheader("Load or delete a saved costing")
        options = {f"{r.client} — {r.project} ({r.display_date})": r.id for r in df.itertuples()}
        choice = st.selectbox("Select a costing", ["—"] + list(options.keys()))
        if choice != "—":
            cid = options[choice]
            lc1, lc2 = st.columns(2)
            if lc1.button("Load into New costing form", use_container_width=True):
                rec = get_costing(cid)
                st.session_state.client = rec["client"]
                st.session_state.project = rec["project"]
                st.session_state.ptype = rec["type"]
                st.session_state.currency = rec["currency"]
                st.session_state.stage = rec["stage"]
                st.session_state.source = rec["source"] or ""
                st.session_state.discount = rec["discount"]
                st.session_state.pm_rate = rec["pm_rate"]
                st.session_state.dev_rate = rec["dev_rate"]
                st.session_state.qc_rate = rec["qc_rate"]
                st.session_state.notes = rec["notes"] or ""
                st.session_state.terms = rec["terms"] or ""
                st.session_state.onetime_df = normalize_df(pd.DataFrame(rec["onetime_rows"]), ONETIME_COLS) if rec["onetime_rows"] else pd.DataFrame([], columns=ONETIME_COLS)
                st.session_state.monthly_df = normalize_df(pd.DataFrame(rec["monthly_rows"]), MONTHLY_COLS) if rec["monthly_rows"] else pd.DataFrame([], columns=MONTHLY_COLS)
                st.success("Loaded — switch to the \"New costing\" tab to edit and re-save.")
            if lc2.button("Delete this costing", use_container_width=True):
                delete_costing(cid)
                st.success("Deleted.")
                st.rerun()
