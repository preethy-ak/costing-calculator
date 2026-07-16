import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import uuid
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

ONETIME_COLS = ["Description", "Qty/Days", "Unit rate", "Apply discount", "Remarks"]
MONTHLY_COLS = ["Description", "Qty", "Unit rate / month", "Apply discount", "Remarks"]

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
    return conn


def save_costing(record: dict):
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO costings
        (id, saved_at, date_label, client, project, type, currency, stage, source,
         discount, pm_rate, dev_rate, qc_rate, onetime_json, monthly_json, notes,
         terms, onetime_total, monthly_total)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record["id"], record["saved_at"], record.get("date_label", ""),
            record["client"], record["project"], record["type"], record["currency"],
            record["stage"], record.get("source", ""), record["discount"],
            record["pm_rate"], record["dev_rate"], record["qc_rate"],
            json.dumps(record["onetime_rows"]), json.dumps(record["monthly_rows"]),
            record.get("notes", ""), record.get("terms", ""),
            record["onetime_total"], record["monthly_total"],
        ),
    )
    conn.commit()
    conn.close()


def load_all_costings() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM costings", conn)
    conn.close()
    if df.empty:
        return df
    df["yr1_value"] = df["onetime_total"] + df["monthly_total"] * 12
    df["display_date"] = df["date_label"].where(df["date_label"].astype(bool), df["saved_at"])
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


# ----------------------------------------------------------------------------
# Row-level helpers
# ----------------------------------------------------------------------------
def onetime_row(desc="", qty=1.0, rate=0.0, disc=False, remarks=""):
    return {"Description": desc, "Qty/Days": qty, "Unit rate": rate,
            "Apply discount": disc, "Remarks": remarks}


def monthly_row(desc="", qty=1.0, rate=0.0, disc=False, remarks=""):
    return {"Description": desc, "Qty": qty, "Unit rate / month": rate,
            "Apply discount": disc, "Remarks": remarks}


def compute_line_totals(df: pd.DataFrame, qty_col: str, rate_col: str, discount_pct: float) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        df["Line total"] = []
        return df
    mult = df["Apply discount"].apply(lambda d: (1 - discount_pct / 100.0) if d else 1.0)
    df["Line total"] = (df[qty_col].fillna(0) * df[rate_col].fillna(0) * mult).round(2)
    return df


def fmt_money(n, ccy="USD"):
    sym = CCY_SYMBOL.get(ccy, "")
    return f"{sym}{n:,.2f}"


# ----------------------------------------------------------------------------
# Templates (patterns pulled from past deal structures)
# ----------------------------------------------------------------------------
def apply_template(kind: str, pm, dev, qc):
    st.session_state.discount = 0.0
    if kind == "api":
        st.session_state.ptype = "Custom API Integration"
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Project Management", 6, pm, False, "Deployed across effort duration"),
            onetime_row("Tech Development", 8, dev, False, "API build + testing hooks"),
            onetime_row("Testing & UAT", 3, qc, False, "Based on past PUMA UAT effort"),
        ])
        st.session_state.monthly_df = pd.DataFrame([
            monthly_row("API Maintenance", 1, 500, False, "Per Graas account / brand"),
        ])
    elif kind == "vend":
        st.session_state.ptype = "POS / Vend-style Integration"
        st.session_state.discount = 30.0
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Project Management", 4, pm, True, "Preferential rate — 30% off standard card"),
            onetime_row("Tech Development", 4, dev, True, "Custom order-pulling integration"),
            onetime_row("Testing & UAT", 2, qc, True, ""),
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
        st.session_state.monthly_df = pd.DataFrame([
            monthly_row("Graas Turbo subscription", 1, 390, False, "Per region bundle"),
        ])
        st.session_state.notes = ("Setup fees apply only to new warehouses; an existing warehouse "
                                   "already on Graas WMS carries no new setup fee. Monthly maintenance "
                                   "is charged per order once live — see tiered fee helper below.")
    elif kind == "extract":
        st.session_state.ptype = "Data Extract Service"
        st.session_state.onetime_df = pd.DataFrame([
            onetime_row("Tech build — extract/API hook", 4, dev, False, "One-time build, up to 3 files/delivery"),
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
                onetime_row("Project Management", 1, 1350, False, "Already reflects 30% preferential discount off standard card"),
                onetime_row("Tech Development", 1, 1350, False, "End-to-end integration & deployment"),
                onetime_row("Testing & UAT", 1, 500, False, "6 working days total effort (dev + testing)"),
            ],
            monthly_rows=[],
            notes=("Confirmed with Kane Gan (Actually Group) via email — covers all offline stores "
                   "(SG, MY, ID) at no extra charge for adding future stores. 50% prepayment before "
                   "project initiation, 50% on completion. Vend integration billed separately from the "
                   "WMS contract."),
            terms="50% prepayment before project initiation, 50% upon project completion",
            onetime_total=3200, monthly_total=0,
            source='Email: "Re: WMS" thread, 18 Jan 2026 — Preethy AK to Kane Gan',
        ),
        dict(
            id="hist_actually_wms", date_label="23 Dec 2025 (email, final proposal)",
            client="Actually Group", project="WMS warehouse setup — Singapore, Malaysia, Indonesia",
            type="WMS Warehouse Setup", currency="SGD", stage="Won", discount=0,
            pm_rate=250, dev_rate=350, qc_rate=180,
            onetime_rows=[
                onetime_row("Warehouse setup — Singapore (new, moving off Humanize)", 1, 1800, False,
                             "New warehouse: account, locations, training"),
                onetime_row("Warehouse setup — Indonesia", 1, 1800, False, ""),
                onetime_row("Warehouse setup — Malaysia (small storeroom, <1000 pcs)", 1, 1000, False,
                             "Client-requested lower fee — smaller storeroom"),
            ],
            monthly_rows=[
                monthly_row("Graas Turbo subscription (SG/MY/ID bundle)", 1, 390, False,
                             "Or SGD 3,900/annual"),
                monthly_row("Per-order fulfilment fee (tiered, illustrative @2,000 orders/mo)", 1, 1.95 * 2000,
                             False, "Up to 2,500 orders @ SGD1.95, overflow @ SGD1.30"),
            ],
            notes=("Negotiated down from an initial SGD 7,800 combined setup-fee quote after Paul Khor "
                   "pushed back comparing to Anchanto/Murho. Existing Humanize (SG) warehouse carries no "
                   "new setup fee while in use; monthly maintenance for Humanize folded into the new "
                   "per-order fee per Kane Gan's request."),
            terms="Setup fees invoiced only when warehouse setup begins",
            onetime_total=1800 + 1800 + 1000, monthly_total=390 + 1.95 * 2000,
            source='Email: "Re: WMS" thread, Nov 2025 - Jan 2026 — Cindy Chiah / Preethy AK to Kane Gan & Paul Khor',
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
    "pm_rate": 250.0, "dev_rate": 350.0, "qc_rate": 180.0, "discount": 0.0,
    "terms": "50% prepayment before start, 50% on completion", "notes": "",
    "onetime_df": pd.DataFrame([
        onetime_row("Project Management", 6, 250, False, "Deployed across effort duration"),
        onetime_row("Tech Development", 8, 350, False, "API build + testing hooks"),
        onetime_row("Testing & UAT", 3, 180, False, "Based on past PUMA UAT effort"),
    ]),
    "monthly_df": pd.DataFrame([
        monthly_row("API Maintenance", 1, 500, False, "Per Graas account / brand"),
    ]),
    "tier_orders": 2000.0, "tier_threshold": 2500.0, "tier_rate1": 1.95, "tier_rate2": 1.30,
    "quote_text": "",
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
            apply_template("api", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate)
            st.rerun()
        if b2.button("Vend / POS Integration", use_container_width=True):
            apply_template("vend", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate)
            st.rerun()
        if b3.button("WMS Warehouse Setup", use_container_width=True):
            apply_template("wms", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate)
            st.rerun()
        if b4.button("Data Extract Service", use_container_width=True):
            apply_template("extract", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate)
            st.rerun()
        if b5.button("Clear form", use_container_width=True):
            apply_template("clear", st.session_state.pm_rate, st.session_state.dev_rate, st.session_state.qc_rate)
            st.rerun()

        st.divider()
        st.subheader("Rate card & discount")
        st.caption("Standard Graas new-development rate card is Project Manager $250/day and Developer "
                   "$350/day. Apply a preferential discount where negotiated (e.g. Actually Vend "
                   "integration was 30% off card).")
        r1, r2, r3, r4 = st.columns(4)
        st.session_state.pm_rate = r1.number_input("PM rate / day", value=float(st.session_state.pm_rate))
        st.session_state.dev_rate = r2.number_input("Developer rate / day", value=float(st.session_state.dev_rate))
        st.session_state.qc_rate = r3.number_input("Tester/QC rate / day", value=float(st.session_state.qc_rate))
        st.session_state.discount = r4.number_input("Rate card discount %", value=float(st.session_state.discount))

        st.divider()
        st.subheader("One-time costs")
        st.caption('Development effort, setup fees, or any one-off line item. Tick "Apply discount" to '
                   "apply the rate-card discount above to that row.")
        onetime_edited = st.data_editor(
            st.session_state.onetime_df, num_rows="dynamic", use_container_width=True,
            key="onetime_editor",
            column_config={
                "Qty/Days": st.column_config.NumberColumn(format="%.2f"),
                "Unit rate": st.column_config.NumberColumn(format="%.2f"),
                "Apply discount": st.column_config.CheckboxColumn(),
            },
        )
        st.session_state.onetime_df = onetime_edited
        onetime_calc = compute_line_totals(onetime_edited, "Qty/Days", "Unit rate", st.session_state.discount)
        onetime_total = onetime_calc["Line total"].sum() if not onetime_calc.empty else 0.0
        if not onetime_calc.empty:
            st.dataframe(onetime_calc[["Description", "Line total"]], use_container_width=True, hide_index=True)

        b_t1, b_t2 = st.columns(2)
        st.session_state.terms = st.text_input("Payment terms", st.session_state.terms)

        st.divider()
        st.subheader("Monthly recurring fees")
        st.caption("Maintenance, subscriptions, per-order fees. These roll into the annualized "
                   "contract value on the right.")
        monthly_edited = st.data_editor(
            st.session_state.monthly_df, num_rows="dynamic", use_container_width=True,
            key="monthly_editor",
            column_config={
                "Qty": st.column_config.NumberColumn(format="%.2f"),
                "Unit rate / month": st.column_config.NumberColumn(format="%.2f"),
                "Apply discount": st.column_config.CheckboxColumn(),
            },
        )
        st.session_state.monthly_df = monthly_edited
        monthly_calc = compute_line_totals(monthly_edited, "Qty", "Unit rate / month", st.session_state.discount)
        monthly_total = monthly_calc["Line total"].sum() if not monthly_calc.empty else 0.0
        if not monthly_calc.empty:
            st.dataframe(monthly_calc[["Description", "Line total"]], use_container_width=True, hide_index=True)

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
        m1.metric("One-time total", fmt_money(onetime_total, ccy))
        m2.metric("Monthly recurring", fmt_money(monthly_total, ccy) + " /mo")
        m1.metric("Year-1 contract value", fmt_money(annual, ccy))
        m2.metric("Total (one-time + monthly)", fmt_money(onetime_total + monthly_total, ccy))

        if st.session_state.currency2 != "—":
            sec_val = (onetime_total + monthly_total) * st.session_state.fx
            st.caption(f"≈ {CCY_SYMBOL.get(st.session_state.currency2,'')}{sec_val:,.2f} in {st.session_state.currency2}")

        st.progress(min(st.session_state.discount / 100.0, 1.0),
                    text=f"{st.session_state.discount:.0f}% rate-card discount applied")

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

        if st.button("Generate client quote", use_container_width=True):
            sym = CCY_SYMBOL.get(ccy, "")
            lines = [
                f"{st.session_state.client or '[Client]'} — {st.session_state.project or '[Project]'}",
                f"({st.session_state.ptype})", "",
            ]
            if not onetime_calc.empty:
                lines.append("One-time costs")
                for _, rr in onetime_calc.iterrows():
                    lines.append(f"{rr['Description']:<38}{sym}{rr['Line total']:,.2f}")
                lines.append(f"{'Total':<38}{sym}{onetime_total:,.2f}")
                lines.append("")
            if not monthly_calc.empty:
                lines.append("Monthly fees")
                for _, rr in monthly_calc.iterrows():
                    lines.append(f"{rr['Description']:<38}{sym}{rr['Line total']:,.2f}")
                lines.append(f"{'Total':<38}{sym}{monthly_total:,.2f} / month")
                lines.append("")
            if st.session_state.discount > 0:
                lines.append(f"The above rate reflects a {st.session_state.discount:.0f}% preferential "
                              f"discount from the Graas standard tech rate card.")
            if st.session_state.terms:
                lines.append(f"Payment terms: {st.session_state.terms}.")
            if st.session_state.notes:
                lines += ["", st.session_state.notes]
            lines += ["", f"Currency: {ccy}. Prices in {ccy} unless noted."]
            st.session_state.quote_text = "\n".join(lines)

        if st.session_state.quote_text:
            st.text_area("Client-ready quote text", st.session_state.quote_text, height=280)
            st.caption("Select all and copy — this matches the Scope/Price/Total format used with clients before.")

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

    df = load_all_costings()

    if df.empty:
        st.info('No costings saved yet. Build one in "New costing" and click "Save to tracker" — or '
                'click "Load / refresh historical deals" above to import past deals.')
    else:
        pipeline = df[~df["stage"].isin(["Lost", "Cancelled"])]["yr1_value"].sum()
        st.metric("Pipeline (Yr-1 value, excluding Lost/Cancelled)", fmt_money(pipeline, "USD"))

        show_cols = ["display_date", "client", "project", "type", "stage",
                     "onetime_total", "monthly_total", "yr1_value", "currency", "source"]
        display_df = df[show_cols].rename(columns={
            "display_date": "Date", "client": "Client", "project": "Project", "type": "Type",
            "stage": "Stage", "onetime_total": "One-time", "monthly_total": "Monthly",
            "yr1_value": "Yr-1 value", "currency": "Ccy", "source": "Source",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)

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
                st.session_state.onetime_df = pd.DataFrame(rec["onetime_rows"]) if rec["onetime_rows"] else pd.DataFrame([], columns=ONETIME_COLS)
                st.session_state.monthly_df = pd.DataFrame(rec["monthly_rows"]) if rec["monthly_rows"] else pd.DataFrame([], columns=MONTHLY_COLS)
                st.success("Loaded — switch to the \"New costing\" tab to edit and re-save.")
            if lc2.button("Delete this costing", use_container_width=True):
                delete_costing(cid)
                st.success("Deleted.")
                st.rerun()
