"""Facets QA dashboard: run results, Facets explorer, 270/271 playground, claims lab.

Local:       streamlit run app/app.py
Databricks:  deployed as a Databricks App (scripts/deploy_databricks.py --app)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

HERE = Path(__file__).resolve().parent
if (HERE.parent / "src" / "facets_qa").exists():  # running from the repo
    sys.path.insert(0, str(HERE.parent / "src"))
    from facets_qa.common.env import load_dotenv

    load_dotenv(HERE.parent / ".env")  # local only; .env is never uploaded with the app

from pydantic import ValidationError  # noqa: E402

from data_source import DatabricksSource, LocalSource, lit  # noqa: E402
from facets_qa.claims.facets_loader import FacetsClaimsIntake  # noqa: E402
from facets_qa.claims.facets_validation import fetch_rows  # noqa: E402
from facets_qa.claims.generator import SyntheticFactory  # noqa: E402
from facets_qa.claims.pipeline import run_scenario  # noqa: E402
from facets_qa.claims.scenarios import SCENARIOS  # noqa: E402
from facets_qa.common.dates import FACETS_HIGH_DATE, to_date  # noqa: E402
from facets_qa.eligibility.builder_270 import build_270, default_envelope  # noqa: E402
from facets_qa.eligibility.decision import decide  # noqa: E402
from facets_qa.eligibility.expected import derive_expected  # noqa: E402
from facets_qa.eligibility.gateway import MockEligibilityGateway, SnapshotMember  # noqa: E402
from facets_qa.eligibility.models import (  # noqa: E402
    SERVICE_TYPE_CODES, Eligibility270Request, FacetsCandidate,
)
from facets_qa.eligibility.parser_271 import parse_271  # noqa: E402
from facets_qa.facets import db as facets_db  # noqa: E402

st.set_page_config(page_title="Facets QA", page_icon="🩺", layout="wide")

BLUE, GOOD, CRITICAL, WARNING = "#2a78d6", "#0ca30c", "#d03b3b", "#fab219"
RESULT_ICON = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "⚠️ ERROR"}
SCHEMA = os.getenv("FACETS_QA_SCHEMA", "workspace.facets_qa")


# --------------------------------------------------------------------- data source
@st.cache_resource(show_spinner=False)
def get_source(kind: str):
    return DatabricksSource(SCHEMA) if kind == "databricks" else LocalSource(HERE.parent / "out")


@st.cache_data(ttl=120, show_spinner="Querying…")
def q(kind: str, sql: str) -> pd.DataFrame:
    return get_source(kind).query(sql)


default_kind = os.getenv("FACETS_QA_SOURCE") or ("databricks" if os.getenv("DATABRICKS_WAREHOUSE_ID") else "local")
with st.sidebar:
    st.title("🩺 Facets QA")
    kind = st.radio("Data source", ["databricks", "local"], index=0 if default_kind == "databricks" else 1,
                    format_func=lambda k: "Databricks SQL warehouse" if k == "databricks" else "Local SQLite mock")
    if st.button("↻ Refresh data"):
        st.cache_data.clear()
    st.caption(f"Schema: `{SCHEMA}`" if kind == "databricks" else "Files: `out/facets_mock.db`")
    st.caption("All members, providers and claims are synthetic test data.")

try:
    src = get_source(kind)
except Exception as exc:  # missing credentials etc.
    st.error(f"Cannot connect to {kind}: {exc}")
    st.stop()

if not src.available():
    st.warning("No local results yet. Run `run-eligibility --members 2000` and `run-claims` first, "
               "or switch the data source to Databricks.")
    st.stop()


def result_label(df: pd.DataFrame, col: str = "result") -> pd.DataFrame:
    if col in df:
        df = df.assign(**{col: df[col].map(lambda r: RESULT_ICON.get(r, r))})
    return df


tab_dash, tab_facets, tab_play, tab_claims = st.tabs(
    ["📊 Run dashboard", "🔎 Facets explorer", "🧪 270/271 playground", "🧾 Claims lab"])

# ===================================================================== dashboard
with tab_dash:
    T = src.t("elig_test_results")
    runs = q(kind, f"""
        SELECT run_id, MIN(run_ts) AS run_ts, COUNT(*) AS total,
               SUM(CASE WHEN result = 'PASS'  THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN result = 'FAIL'  THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN result = 'ERROR' THEN 1 ELSE 0 END) AS errored,
               SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END) AS retried,
               AVG(latency_ms) AS avg_latency_ms
        FROM {T} GROUP BY run_id ORDER BY run_ts DESC""")
    if runs.empty:
        st.info("No eligibility runs yet.")
    else:
        runs["pass_rate"] = (100 * runs["passed"] / runs["total"]).round(2)
        run_id = st.selectbox("Eligibility run", runs["run_id"], index=0,
                              format_func=lambda r: f"{r}  ·  {runs.set_index('run_id').loc[r, 'run_ts']}")
        cur = runs.set_index("run_id").loc[run_id]
        prev = runs[runs["run_ts"] < cur["run_ts"]].head(1)

        c = st.columns(5)
        c[0].metric("Eligibility checks", f"{int(cur.total):,}")
        c[1].metric("Pass rate", f"{cur.pass_rate:.2f}%",
                    None if prev.empty else f"{cur.pass_rate - prev.iloc[0].pass_rate:+.2f} pts")
        c[2].metric("❌ FAIL", int(cur.failed))
        c[3].metric("⚠️ ERROR (gateway)", int(cur.errored))
        c[4].metric("Retried checks", f"{int(cur.retried):,}", help="Transient gateway failures recovered by "
                    "exponential backoff")

        by_scn = q(kind, f"""
            SELECT scenario,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) AS passed,
                   SUM(CASE WHEN result <> 'PASS' THEN 1 ELSE 0 END) AS not_passed
            FROM {T} WHERE run_id = {lit(run_id)} GROUP BY scenario""")
        by_scn["pass_rate_%"] = (100 * by_scn["passed"] / by_scn["total"]).round(2)
        by_scn = by_scn.sort_values("total", ascending=False)

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Checks by scenario")
            bars = alt.Chart(by_scn).mark_bar(color=BLUE, cornerRadiusEnd=4, size=18).encode(
                x=alt.X("total:Q", title="Eligibility checks"),
                y=alt.Y("scenario:N", sort="-x", title=None),
                tooltip=["scenario", alt.Tooltip("total:Q", format=","), alt.Tooltip("passed:Q", format=","),
                         alt.Tooltip("not_passed:Q", title="fail + error"), "pass_rate_%"],
            )
            labels = bars.mark_text(align="left", dx=4, color="gray").encode(text=alt.Text("total:Q", format=","))
            st.altair_chart((bars + labels).properties(height=34 * len(by_scn) + 20), width="stretch")
        with right:
            st.subheader("Pass rate by scenario")
            st.dataframe(by_scn[["scenario", "total", "not_passed", "pass_rate_%"]].rename(
                columns={"not_passed": "fail+error"}), hide_index=True, width="stretch")

        if len(runs) > 1:
            st.subheader("Pass-rate trend across runs")
            trend = runs.sort_values("run_ts").assign(run_ts=lambda d: pd.to_datetime(d["run_ts"]))
            line = alt.Chart(trend).mark_line(color=BLUE, strokeWidth=2, point=alt.OverlayMarkDef(
                color=BLUE, size=70)).encode(
                x=alt.X("run_ts:T", title="Run time"),
                y=alt.Y("pass_rate:Q", title="Pass rate (%)", scale=alt.Scale(zero=False)),
                tooltip=["run_id", alt.Tooltip("pass_rate:Q", format=".2f"), alt.Tooltip("total:Q", format=","),
                         "failed", "errored"],
            )
            st.altair_chart(line.properties(height=240), width="stretch")

        st.subheader("Failures and errors")
        fails = q(kind, f"""
            SELECT member_id, scenario, expected_status, actual_status, expected_aaa, actual_aaa,
                   result, failure_reason, attempts, trace_number
            FROM {T} WHERE run_id = {lit(run_id)} AND result <> 'PASS' ORDER BY scenario LIMIT 1000""")
        if fails.empty:
            st.success("Every eligibility check in this run passed.")
        else:
            st.dataframe(result_label(fails), hide_index=True, width="stretch")
            st.download_button("Download failures (CSV)", fails.to_csv(index=False), f"{run_id}_failures.csv")

# ===================================================================== Facets explorer
with tab_facets:
    st.subheader("Member lookup")
    sample = q(kind, f"SELECT SBSB_ID FROM {src.t('cmc_sbsb_subsc')} ORDER BY SBSB_ID LIMIT 1")
    default_id = sample.iloc[0, 0] if not sample.empty else ""
    a, b = st.columns([2, 1])
    member_id = a.text_input("Subscriber ID (SBSB_ID)", default_id, key="fx_member")
    svc = b.date_input("Service date", date.today(), key="fx_svc")

    def load_member(mid: str):
        mem = q(kind, f"""
            SELECT m.MEME_CK, s.SBSB_ID, m.MEME_FIRST_NAME, m.MEME_LAST_NAME, m.MEME_BIRTH_DT, m.MEME_SEX
            FROM {src.t('cmc_meme_member')} m JOIN {src.t('cmc_sbsb_subsc')} s ON s.SBSB_CK = m.SBSB_CK
            WHERE s.SBSB_ID = {lit(mid)} AND m.MEME_SFX = 0""")
        spans = q(kind, f"""
            SELECT MEPE_EFF_DT, MEPE_TERM_DT, MEPE_ELIG_IND, CSPD_CAT, PDPD_ID
            FROM {src.t('cmc_mepe_prcs_elig')} WHERE MEME_CK = {int(mem.iloc[0].meme_ck)}
            ORDER BY MEPE_EFF_DT""") if not mem.empty else pd.DataFrame()
        return mem, spans

    try:
        mem, spans = load_member(member_id)
    except ValueError as exc:
        st.error(str(exc))
        mem, spans = pd.DataFrame(), pd.DataFrame()
    if member_id and mem.empty:
        st.warning("Not in Facets — a 270 for this ID should come back with AAA 75 (or 72 if malformed).")
    elif not mem.empty:
        st.dataframe(mem, hide_index=True, width="stretch")
        st.markdown("**CMC_MEPE_PRCS_ELIG spans**")
        st.dataframe(spans, hide_index=True, width="stretch")
        cand = FacetsCandidate(
            meme_ck=int(mem.iloc[0].meme_ck), member_id=member_id, first_name=mem.iloc[0].meme_first_name,
            last_name=mem.iloc[0].meme_last_name, dob=to_date(mem.iloc[0].meme_birth_dt), service_date=svc,
            spans=tuple({"eff_dt": to_date(r.mepe_eff_dt), "term_dt": to_date(r.mepe_term_dt),
                         "elig_ind": r.mepe_elig_ind, "cspd_cat": r.cspd_cat, "pdpd_id": r.pdpd_id}
                        for r in spans.itertuples()),
        ) if to_date(mem.iloc[0].meme_birth_dt) <= svc else None
        if cand:
            exp = derive_expected(cand)
            st.info(f"**Expected 271 on {svc}: {exp.status}** — scenario `{exp.scenario.value}` "
                    f"(derived from the spans above; Facets is the source of truth)")
        hist = q(kind, f"""SELECT run_id, run_ts, scenario, expected_status, actual_status, result, failure_reason
                          FROM {src.t('elig_test_results')} WHERE member_id = {lit(member_id)}
                          ORDER BY run_ts DESC LIMIT 20""")
        st.markdown("**Test history for this member**")
        st.dataframe(result_label(hist), hide_index=True, width="stretch")

    st.divider()
    st.subheader("Table browser")
    tables = ["cmc_sbsb_subsc", "cmc_meme_member", "cmc_mepe_prcs_elig", "cmc_prpr_prov", "cmc_clcl_claim",
              "cmc_cdml_cl_line", "cmc_clhp_hosp", "elig_test_results", "claim_test_results"]
    tbl = st.selectbox("Table", tables)
    try:
        n = q(kind, f"SELECT COUNT(*) AS n FROM {src.t(tbl)}").iloc[0, 0]
        st.caption(f"{int(n):,} rows · showing first 200")
        preview = q(kind, f"SELECT * FROM {src.t(tbl)} LIMIT 200")
        st.dataframe(preview.drop(columns=[c for c in ("x12",) if c in preview]), hide_index=True,
                     width="stretch")
    except Exception as exc:
        st.warning(f"Table not available in this source: {exc}")

# ===================================================================== 270/271 playground
with tab_play:
    st.subheader("Send a 270, inspect the 271")
    st.caption("Builds a real X12 270 from Facets data, answers it with the mock clearinghouse, parses the 271 "
               "and runs the same PASS/FAIL decision as the regression job.")
    c1, c2, c3 = st.columns(3)
    p_member = c1.text_input("Member ID (NM109)", default_id, key="pg_member",
                             help="Try 990000001 (not found → AAA 75) or INV00042 (invalid → AAA 72)")
    p_svc = c2.date_input("Service date (DTP*291)", date.today(), key="pg_svc")
    p_npi = c3.text_input("Provider NPI (NM1*1P)", "1234567893", help="Must pass the Luhn check digit")
    c4, c5 = st.columns([2, 1])
    p_stc = c4.multiselect("Service type codes (EQ01)", sorted(SERVICE_TYPE_CODES), default=["30"])
    p_drift = c5.toggle("Simulate gateway defect", help="Gateway answers the opposite of Facets for this member")

    if st.button("Send 270", type="primary"):
        try:
            mem, spans = load_member(p_member)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        in_facets = not mem.empty
        if in_facets:
            row = mem.iloc[0]
            span_dicts = [{"eff_dt": to_date(r.mepe_eff_dt), "term_dt": to_date(r.mepe_term_dt),
                           "elig_ind": r.mepe_elig_ind, "cspd_cat": r.cspd_cat, "pdpd_id": r.pdpd_id}
                          for r in spans.itertuples()]
            first, last, dob = row.meme_first_name, row.meme_last_name, to_date(row.meme_birth_dt)
        else:
            span_dicts, first, last, dob = [], "PROBE", "NEGATIVE", date(1980, 1, 1)
        probe = None if in_facets else ("member_not_found" if p_member.isdigit() and len(p_member) == 9
                                        else "invalid_member_id")
        try:
            cand = FacetsCandidate(meme_ck=int(row.meme_ck) if in_facets else None, member_id=p_member,
                                   first_name=first, last_name=last, dob=dob, service_date=p_svc,
                                   in_facets=in_facets, probe=probe, spans=tuple(span_dicts))
            req = Eligibility270Request(
                trace_number=f"PLAY-{p_member}"[:50], payer_id="FACETS01", payer_name="FACETS HEALTH PLAN",
                provider_npi=p_npi, provider_name="FACETS QA CLINIC", member_id=p_member, member_first=first,
                member_last=last, member_dob=dob, service_date=p_svc, service_type_codes=tuple(p_stc))
        except ValidationError as exc:
            st.error("Pydantic contract rejected the request before it was sent:")
            st.dataframe(pd.DataFrame([{"field": ".".join(map(str, e["loc"])), "rule": e["msg"]}
                                       for e in exc.errors()]), hide_index=True)
            st.stop()

        expected = derive_expected(cand)
        snapshot = {p_member: SnapshotMember(p_member, first, last, dob, tuple(
            (s["eff_dt"], s["term_dt"], s["elig_ind"], s["pdpd_id"] or "") for s in span_dicts
            if s["cspd_cat"] == "M"))} if in_facets else {}
        gw = MockEligibilityGateway(snapshot, drift_member_ids=frozenset({p_member}) if p_drift else frozenset())
        x270 = build_270(req, default_envelope(), 1)
        x271 = gw.send(x270)
        resp = parse_271(x271)
        verdict = decide(expected, resp, req.trace_number, p_member)

        icon = {"PASS": "✅", "FAIL": "❌", "ERROR": "⚠️"}[verdict.result]
        msg = (f"{icon} **{verdict.result}** — expected **{expected.status}**"
               f"{' (AAA ' + expected.aaa_reason + ')' if expected.aaa_reason else ''} "
               f"[{expected.scenario.value}], 271 says **{resp.coverage_status}**"
               f"{' (AAA ' + resp.primary_reject_reason + ')' if resp.primary_reject_reason else ''}")
        (st.success if verdict.result == "PASS" else st.error)(msg + (f"\n\n{verdict.reason}" if verdict.reason else ""))

        l, r = st.columns(2)
        l.markdown("**270 request (005010X279A1)**")
        l.code(x270, language=None)
        r.markdown("**271 response**")
        r.code(x271, language=None)
        st.markdown("**Parsed 271 (Pydantic `Eligibility271Response`)**")
        st.json(json.loads(resp.model_dump_json()))

    with st.expander("X12 270/271 segment guide"):
        st.markdown("""
| Segment | Loop | Meaning |
|---|---|---|
| `ISA` / `IEA` | envelope | Interchange header/trailer (fixed width, control number) |
| `GS` / `GE` | envelope | Functional group — `HS` = 270, `HB` = 271 |
| `ST` / `SE` | envelope | Transaction set; SE01 = segment count |
| `BHT` | header | `0022` + purpose `13` request / `11` response |
| `HL*…*20/21/22` | 2000A/B/C | Information source (payer) / receiver (provider) / subscriber |
| `NM1*PR` / `NM1*1P` / `NM1*IL` | 2100A/B/C | Payer / provider (XX = NPI) / subscriber (MI = member ID) |
| `TRN` | 2000C | Trace number — `1` in the 270, echoed as `2` in the 271 |
| `DMG*D8` | 2100C | Date of birth |
| `DTP*291` / `346` | 2100C/2110C | Plan date (D8 or RD8 range) / plan begin |
| `EQ` | 2110C | Service type codes requested (30 = health benefit plan coverage) |
| `EB*1` / `EB*6` | 2110C | Active coverage / inactive |
| `AAA*N**72` / `75` | 2100C | Rejection: invalid member ID / subscriber not found |
""")

# ===================================================================== claims lab
with tab_claims:
    st.subheader("Run a claims scenario")
    names = [s.name for s in SCENARIOS]
    sc_name = st.selectbox("Scenario", names, format_func=lambda n: (
        f"{'✔️' if next(s for s in SCENARIOS if s.name == n).polarity == 'positive' else '🚫'}  {n}"))
    sc = next(s for s in SCENARIOS if s.name == sc_name)
    st.caption(f"**{sc.claim_type} · {sc.polarity}** — {sc.description}"
               + (f" · must fail with: “{sc.expected_error}”" if sc.expected_error else ""))
    defect = st.selectbox("Inject a Facets intake defect", ["none", *sorted(FacetsClaimsIntake.DEFECTS)],
                          disabled=sc.polarity == "negative",
                          help="Shows how the field-level Facets validation reports a mismatch")

    if st.button("Run scenario", type="primary"):
        conn = facets_db.connect(":memory:")
        facets_db.create_schema(conn)
        factory = SyntheticFactory(837)
        payload = sc.build(factory, date.today() - timedelta(days=14))
        intake = FacetsClaimsIntake(conn, defects={payload["claim_id"]: defect} if defect != "none" else None)
        res = run_scenario(sc, conn, intake, SyntheticFactory(837), date.today() - timedelta(days=14), None, 1)

        (st.success if res.result == "PASS" else st.error)(f"{RESULT_ICON[res.result]} — {res.detail}")
        with st.expander("Claim payload (input to the Pydantic discriminated union)"):
            st.json(json.loads(json.dumps(payload, default=str)))
        if res.x12:
            l, r = st.columns([1, 1])
            l.markdown(f"**Generated {sc.claim_type}**")
            l.code(res.x12, language=None)
            hdr, lines, hosp = fetch_rows(conn, res.clcl_id)
            r.markdown("**CMC_CLCL_CLAIM**")
            r.dataframe(pd.DataFrame([hdr.model_dump()]).T.rename(columns={0: "value"}).astype(str),
                        width="stretch")
            r.markdown("**CMC_CDML_CL_LINE**")
            r.dataframe(pd.DataFrame([ln.model_dump() for ln in lines]).astype(str), hide_index=True,
                        width="stretch")
            if hosp:
                r.markdown("**CMC_CLHP_HOSP**")
                r.dataframe(pd.DataFrame([hosp.model_dump()]).astype(str), hide_index=True,
                            width="stretch")

    st.divider()
    st.subheader("Latest claims regression run")
    CT = src.t("claim_test_results")
    try:
        latest = q(kind, f"""SELECT run_id, run_ts, scenario, claim_type, polarity, clcl_id, result, detail
                            FROM {CT} WHERE run_id = (SELECT MAX(run_id) FROM {CT}) ORDER BY polarity DESC, scenario""")
        if latest.empty:
            st.info("No claims runs yet.")
        else:
            ok = (latest["result"] == "PASS").sum()
            st.caption(f"Run `{latest.iloc[0].run_id}` · {ok}/{len(latest)} scenarios passed")
            st.dataframe(result_label(latest.drop(columns=["run_id"])), hide_index=True, width="stretch")
    except Exception as exc:
        st.info(f"Claims results not available in this source ({exc}).")
