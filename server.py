import sqlite3
import math
import random
import csv
import io
from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="IS 17021 Fuel Adulteration & Ethanol Decoupling Workbench")

DB_FILE = "fuel_telemetry.db"

# Reference Physical Constants at 15°C
RHO_PETROL = 0.7400
EPS_PETROL = 2.10

RHO_ETHANOL = 0.7890
EPS_ETHANOL = 24.50

RHO_KEROSENE = 0.8100
EPS_KEROSENE = 2.20

# Shared Setpoint State for Edge Node Polling
CURRENT_SETPOINT = {
    "temp_c": 25.0,
    "ethanol_pct": 20.0,
    "kerosene_pct": 0.0,
    "batch_offset": 0.0,
    "solvent_shift": 0.0,
    "noise_enabled": True,
    "edge_case_tag": "NONE",
    "seq": 365
}

LATEST_EDGE_READING = {}


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS inspection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            node_id TEXT,
            temp_c REAL,
            mix_label TEXT,
            rho_15 REAL,
            eps_obs REAL,
            est_e_pct REAL,
            residual_drho REAL,
            threshold_rel TEXT,
            verdict TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


class EvalRequest(BaseModel):
    temp_c: float = 25.0
    ethanol_pct: float = 20.0
    kerosene_pct: float = 0.0
    batch_offset: float = 0.0
    solvent_shift: float = 0.0
    noise_enabled: bool = True
    edge_case_tag: str = "NONE"
    node_id: str = "ESP32-PUN-084"
    record_to_db: bool = False


def evaluate_physics(req: EvalRequest):
    CURRENT_SETPOINT["seq"] += 1
    # Allow math up to 35% internally so it doesn't break when slider hits 30%
    e_frac = max(0.0, min(0.35, req.ethanol_pct / 100.0))
    k_frac = max(0.0, min(0.30, req.kerosene_pct / 100.0))

    # 1. Dielectric Simulation (Calibrated Inversion Manifold + Noise)
    eps_15_clean = EPS_PETROL + e_frac * (EPS_ETHANOL - EPS_PETROL) + k_frac * (EPS_KEROSENE - EPS_PETROL)
    noise_eps = random.gauss(0.0, 0.04) if req.noise_enabled else 0.0
    eps_obs = round(eps_15_clean + noise_eps, 2)

    # Stage 1: Estimate Ethanol (Ê) from Dielectric Inversion
    est_e_frac = (eps_obs - EPS_PETROL) / (EPS_ETHANOL - EPS_PETROL)
    est_e_frac = max(0.0, min(0.40, est_e_frac))
    est_e_pct = round(est_e_frac * 100.0, 1)
    est_e_used = round(est_e_pct / 100.0, 3)

    # Stage 2: Compute Expected Clean Density (ρ_exp) at 15°C
    rho_exp = round((1.0 - est_e_used) * RHO_PETROL + est_e_used * RHO_ETHANOL, 4)

    # Simulate True Chamber Density normalized to 15°C
    true_clean_rho = (1.0 - e_frac) * RHO_PETROL + e_frac * RHO_ETHANOL
    kero_shift = (req.kerosene_pct) * 0.0007
    noise_rho = random.gauss(0.0, 0.0003) if req.noise_enabled else 0.0

    # Lock exact boundary values when noise is disabled on boundary presets
    if not req.noise_enabled and req.edge_case_tag == "EQUAL_LOWER":
        rho_comp15 = round(rho_exp + 0.0020, 4)
    elif not req.noise_enabled and req.edge_case_tag == "EQUAL_UPPER":
        rho_comp15 = round(rho_exp + 0.0045, 4)
    else:
        rho_comp15 = round(true_clean_rho + kero_shift + req.batch_offset + req.solvent_shift + noise_rho, 4)

    # Stage 3: Isolate Adulteration Residual (Δρ)
    delta_rho = round(rho_comp15 - rho_exp, 4)

    # ---------------------------------------------------------
    # DECISION MATRIX: Kerosene Density vs Ethanol Limits
    # ---------------------------------------------------------
    is_exact_lower = (abs(delta_rho - 0.0020) < 1e-6)
    is_exact_upper = (abs(delta_rho - 0.0045) < 1e-6)

    # Evaluate Density Status (Kerosene)
    kero_state = "PASS"
    if delta_rho < -0.0045:
        kero_state = "LOW"
    elif is_exact_lower or delta_rho <= 0.0020:
        kero_state = "PASS"
    elif is_exact_upper or delta_rho <= 0.0045:
        kero_state = "ABSTAIN"
    else:
        kero_state = "FAIL"

    # Evaluate Ethanol Status
    eth_state = "PASS"
    if est_e_pct > 25.0:
        eth_state = "FAIL"
    elif est_e_pct > 20.5:
        eth_state = "WARN"

    # Combine Verdicts for UI Severity
    if kero_state == "FAIL":
        verdict = "ADULTERATED"
        ui_class = "ADULTERATED"
        banner_text = "RESULT: HEAVY-HYDROCARBON ADULTERATION SUSPECTED"
    elif kero_state == "LOW":
        verdict = "LOW ANOMALY"
        ui_class = "LOW"
        banner_text = "RESULT: EXTREME NEGATIVE RESIDUAL (Δρ < -0.0045) — SOLVENT SUSPECTED"
    elif eth_state == "FAIL":
        verdict = "ETHANOL FAIL"
        ui_class = "ADULTERATED"
        banner_text = f"RESULT: ILLEGAL ETHANOL LEVEL ({est_e_pct}%) — REGULATORY FAIL"
    elif kero_state == "ABSTAIN":
        verdict = "ABSTAIN / LAB"
        ui_class = "ABSTAIN"
        banner_text = "RESULT: BORDERLINE DENSITY — SEND TO LAB (ABSTAIN)"
    elif eth_state == "WARN":
        verdict = "ETHANOL WARN"
        ui_class = "ABSTAIN"
        banner_text = f"RESULT: ETHANOL ({est_e_pct}%) EXCEEDS E20 STANDARD — WARNING"
    else:
        verdict = "PASS"
        ui_class = "PASS"
        banner_text = "RESULT: COMPLIANT FUEL BLEND (PASS)"

    # Append extra warning if both are violated
    if kero_state in ["FAIL", "ABSTAIN"] and eth_state in ["FAIL", "WARN"]:
        banner_text += f" | PLUS ETHANOL WARNING ({est_e_pct}%)"

    # Preserving existing relational operator explanations for the dashboard
    if is_exact_lower:
        rel_operator = "EQUAL (= +0.0020 g/cm³)"
        valid_reason = "EQUAL TO PASS LIMIT (Δρ = +0.0020 g/cm³): The decision rule uses an inclusive bound (Δρ ≤ +0.0020). Why +0.0020? Legal crude oil batches vary naturally up to +0.0020 g/cm³ (Heavy Refinery Batch). Setting the boundary here prevents false alarms."
    elif is_exact_upper:
        rel_operator = "EQUAL (= +0.0045 g/cm³)"
        valid_reason = "EQUAL TO ABSTAIN CEILING (Δρ = +0.0045 g/cm³): Adulteration fail requires strictly Δρ > +0.0045 g/cm³. This represents the maximum worst-case stack-up of a Heavy Refinery Batch (+0.0020) plus 3σ hardware tolerance (+0.0025)."
    elif delta_rho < -0.0045:
        rel_operator = "EXTREME LESSER (Δρ < -0.0045 g/cm³)"
        valid_reason = "LESSER THAN LOW ANOMALY LIMIT (Δρ < -0.0045 g/cm³): Measured density is abnormally lighter than expected. Caused by light hydrocarbon solvent adulteration (e.g., Naphtha/Hexane) or vapour cavitation bubbles in the chamber."
    elif delta_rho < 0.0020:
        rel_operator = "LESSER (Δρ < +0.0020 g/cm³)"
        valid_reason = "LESSER THAN PASS LIMIT (Δρ < +0.0020 g/cm³): Actual measured density matches the theoretical clean E-blend density. Minor non-zero deviations are normal and caused only by Gaussian sensor noise (σρ = 0.0003) or slight refinery batch variation."
    elif 0.0020 < delta_rho <= 0.0045:
        rel_operator = "BIGGER [Zone 1] (+0.0020 < Δρ ≤ +0.0045)"
        valid_reason = "BIGGER THAN PASS LIMIT (+0.0020 < Δρ ≤ +0.0045 g/cm³): Every 1% Kerosene shifts Δρ upward by +0.0007 g/cm³. In this grey zone, a 3–5% kerosene mix overlaps statistically with a heavy refinery batch, requiring lab verification."
    else:
        rel_operator = "BIGGER [Zone 2] (Δρ > +0.0045 g/cm³)"
        valid_reason = "BIGGER THAN FAIL LIMIT (Δρ > +0.0045 g/cm³): Kerosene (0.810 g/cm³) is much heavier than petrol (0.740 g/cm³) but invisible to the dielectric probe. A residual above +0.0045 g/cm³ exceeds all tolerances, proving heavy adulteration."

    edge_explanation = "Nominal operating region — no boundary masking or anomaly active."
    if req.edge_case_tag == "EQUAL_LOWER":
        edge_explanation = "BOUNDARY CASE (Exact Collision at +0.0020): ~2.8% Kerosene produces a +0.0020 g/cm³ shift. Field sensors cannot separate <2.8% kerosene from a legal +0.0020 heavy refinery batch."
    elif req.edge_case_tag == "EQUAL_UPPER":
        edge_explanation = "BOUNDARY CASE (Upper Abstain Cutoff at +0.0045): Exact tipping point (~6.4% Kerosene). Adding even 0.1% more kerosene pushes Δρ > +0.0045 into immediate ADULTERATED status."
    elif req.edge_case_tag == "MASKING_LIGHT":
        edge_explanation = "MASKING SCENARIO (Light Refinery Batch): Fuel comes from a Light Batch (-0.0020 g/cm³) and contains 4.5% Kerosene (+0.0032 g/cm³). They cancel out to +0.0012 g/cm³ (False PASS) unless batch offset is calibrated."
    elif req.edge_case_tag == "HEAVY_GUARD":
        edge_explanation = "FALSE-POSITIVE GUARD (Heavy Batch + Noise): Pure E20 fuel (0% Kerosene) from a Heavy Batch (+0.0020 g/cm³) with a +0.0005 sensor spike gives Δρ = +0.0025 g/cm³. The Abstain band prevents false accusation."
    elif req.edge_case_tag == "LOW_ANOMALY":
        edge_explanation = "NEGATIVE RESIDUAL ANOMALY (Light Solvent / Cavitation): Adulteration with lighter naphtha solvent or air bubbles drops density far below expected clean density (Δρ < -0.0045 g/cm³)."

    if req.record_to_db:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        mix_str = f"E{req.ethanol_pct:.1f}% / K{req.kerosene_pct:.1f}%"
        c.execute("""
            INSERT INTO inspection_log
            (timestamp, node_id, temp_c, mix_label, rho_15, eps_obs, est_e_pct, residual_drho, threshold_rel, verdict)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%H:%M:%S"),
            req.node_id,
            req.temp_c,
            mix_str,
            rho_comp15,
            eps_obs,
            est_e_pct,
            delta_rho,
            rel_operator,
            verdict
        ))
        conn.commit()
        conn.close()

    return {
        "seq": CURRENT_SETPOINT["seq"],
        "timestamp": datetime.now().strftime("%I:%M:%S %p"),
        "rho_comp15": rho_comp15,
        "eps_obs": eps_obs,
        "est_e_pct": est_e_pct,
        "est_e_frac": est_e_used,
        "rho_exp": rho_exp,
        "delta_rho": delta_rho,
        "verdict": verdict,
        "ui_class": ui_class,
        "banner_text": banner_text,
        "rel_operator": rel_operator,
        "valid_reason": valid_reason,
        "edge_explanation": edge_explanation
    }


@app.post("/api/telemetry/evaluate")
def api_evaluate(req: EvalRequest):
    CURRENT_SETPOINT.update({
        "temp_c": req.temp_c,
        "ethanol_pct": req.ethanol_pct,
        "kerosene_pct": req.kerosene_pct,
        "batch_offset": req.batch_offset,
        "solvent_shift": req.solvent_shift,
        "noise_enabled": req.noise_enabled,
        "edge_case_tag": req.edge_case_tag
    })
    res = evaluate_physics(req)
    return JSONResponse(res)


@app.get("/api/edge/setpoint")
def api_get_setpoint():
    return JSONResponse(CURRENT_SETPOINT)


@app.post("/api/edge/ingest")
def api_edge_ingest(req: EvalRequest):
    global LATEST_EDGE_READING
    LATEST_EDGE_READING = evaluate_physics(req)
    return JSONResponse({"status": "ok", "verdict": LATEST_EDGE_READING["verdict"]})


@app.get("/api/logs")
def api_get_logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, timestamp, node_id, temp_c, mix_label, rho_15, eps_obs, est_e_pct, residual_drho, threshold_rel, verdict
        FROM inspection_log ORDER BY id DESC LIMIT 25
    """)
    rows = c.fetchall()
    conn.close()
    return JSONResponse({"logs": rows})


@app.post("/api/logs/clear")
def api_clear_logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM inspection_log")
    conn.commit()
    conn.close()
    return JSONResponse({"status": "cleared"})


@app.get("/api/logs/export")
def api_export_csv():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM inspection_log ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Time", "Node", "Temp_C", "Set_Mix", "Measured_Rho15", "Measured_Eps", "Est_E_Pct", "Residual_dRho", "Relational_State", "Verdict"])
    writer.writerows(rows)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fuel_telemetry_export.csv"}
    )


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>IS 17021 Fuel Adulteration & Ethanol Decoupling Workbench</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { margin: 0; background: #f3f5f8; color: #1e293b; font-size: 13px; }
  .topbar { background: #fff; border-bottom: 1px solid #dce1e7; padding: 10px 18px; display: flex; justify-content: space-between; align-items: center; }
  .topbar h1 { margin: 0; font-size: 16px; font-weight: 700; color: #0f172a; }
  .topbar p { margin: 2px 0 0 0; font-size: 11.5px; color: #64748b; }
  .edge-badge { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; padding: 4px 10px; border-radius: 4px; font-family: monospace; font-size: 11.5px; font-weight: 600; }
  .btn-outline { background: #fff; border: 1px solid #cbd5e1; padding: 5px 10px; border-radius: 4px; cursor: pointer; font-size: 11.5px; font-weight: 600; }
  .container { display: grid; grid-template-columns: 300px 1fr; gap: 14px; padding: 14px; max-width: 1600px; margin: 0 auto; }
  .card { background: #fff; border: 1px solid #dce1e7; border-radius: 6px; padding: 12px; margin-bottom: 12px; }
  .card-title { font-size: 12px; font-weight: 700; color: #334155; margin: 0 0 10px 0; border-bottom: 1px solid #f1f5f9; padding-bottom: 6px; }
  .preset-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .preset-btn { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 4px; padding: 7px 6px; font-size: 11.5px; cursor: pointer; text-align: center; font-weight: 500; transition: 0.15s; }
  .preset-btn:hover { background: #e2e8f0; }
  .preset-btn.rel { background: #eff6ff; border-color: #93c5fd; color: #1e40af; font-weight: 600; }
  .preset-btn.anom { background: #fef3c7; border-color: #fcd34d; color: #92400e; font-weight: 600; }
  .param-row { margin-bottom: 10px; }
  .param-label { display: flex; justify-content: space-between; font-size: 11.5px; font-weight: 600; margin-bottom: 4px; }
  .slider-wrap { display: flex; align-items: center; gap: 8px; }
  .slider-wrap input[type=range] { flex: 1; }
  .slider-wrap input[type=number] { width: 58px; padding: 3px 5px; border: 1px solid #cbd5e1; border-radius: 4px; text-align: right; font-size: 12px; }
  select { width: 100%; padding: 6px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 11.5px; background: #fff; }
  .btn-primary { width: 100%; background: #0284c7; color: #fff; border: none; padding: 8px; border-radius: 4px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  .btn-primary:hover { background: #0369a1; }
  .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 12px; }
  .metric-card { background: #fff; border: 1px solid #dce1e7; border-radius: 6px; padding: 12px; }
  .metric-label { font-size: 10.5px; font-weight: 700; color: #64748b; text-transform: uppercase; }
  .metric-val { font-size: 24px; font-weight: 800; margin: 4px 0; font-family: "Segoe UI", sans-serif; }
  .metric-sub { font-size: 11px; color: #64748b; }
  .banner { padding: 10px 14px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border: 1px solid; }
  .banner.PASS { background: #f0fdf4; border-color: #86efac; color: #166534; }
  .banner.ABSTAIN { background: #fffbeb; border-color: #fde047; color: #92400e; }
  .banner.ADULTERATED { background: #fef2f2; border-color: #fca5a5; color: #991b1b; }
  .banner.LOW { background: #f3e8ff; border-color: #d8b4fe; color: #6b21a8; }
  .verdict-pill { padding: 4px 12px; border-radius: 4px; font-weight: 800; font-size: 11.5px; border: 1px solid currentColor; }
  .diag-box { background: #f8fafc; border: 1px solid #cbd5e1; border-left: 4px solid #2563eb; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; }
  .diag-grid { display: grid; grid-template-columns: 220px 1fr 1fr; gap: 12px; align-items: start; }
  .op-badge { display: inline-block; background: #1e293b; color: #fff; font-family: monospace; font-size: 12px; font-weight: 700; padding: 5px 10px; border-radius: 4px; margin-top: 4px; }
  .stage-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; background: #f8fafc; padding: 10px; border-radius: 4px; border: 1px solid #e2e8f0; font-family: monospace; font-size: 12px; }
  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid #e2e8f0; }
  th { color: #64748b; font-weight: 600; background: #f8fafc; }
</style>
</head>
<body>

<div class="topbar">
  <div>
    <h1>Fuel Adulteration & Ethanol Decoupling Workbench</h1>
    <p>IoT Sensor Simulation & Two-Stage Residual Verification (IS 17021:2026 / E20 Standard)</p>
  </div>
  <div style="display:flex; align-items:center; gap:12px;">
    <span class="edge-badge" id="seqBadge">IOT EDGE: ESP32-PUN-084 [SEQ #365]</span>
    <span id="clockSpan" style="color:#64748b; font-size:12px;">--:--:--</span>
    <button class="btn-outline" onclick="window.location.href='/api/logs/export'">Export SQLite (.CSV)</button>
  </div>
</div>

<div class="container">
  <!-- LEFT SIDEBAR -->
  <div>
    <div class="card">
      <div class="card-title">1A. Standard Test Cases (Presets)</div>
      <div class="preset-grid">
        <button class="preset-btn" onclick="applyPreset(25, 0, 0, 0.0, 0.0, true, 'NONE')">Pure Petrol (E0)</button>
        <button class="preset-btn" onclick="applyPreset(25, 20, 0, 0.0, 0.0, true, 'NONE')">Legal E20 Blend</button>
        <button class="preset-btn" onclick="applyPreset(25, 15, 4, 0.0, 0.0, true, 'NONE')">Borderline (4% Kero)</button>
        <button class="preset-btn" onclick="applyPreset(25, 20, 12, 0.0, 0.0, true, 'NONE')">Adulterated (12% Kero)</button>
      </div>

      <div class="card-title" style="margin-top:12px;">1B. Residual Boundary States (<, =, >)</div>
      <div class="preset-grid">
        <button class="preset-btn rel" onclick="applyPreset(25, 14.5, 0, 0.0, 0.0, false, 'NONE')">Lesser (< +0.0020)</button>
        <button class="preset-btn rel" onclick="applyPreset(25, 10, 2.85, 0.0, 0.0, false, 'EQUAL_LOWER')">Equal (= +0.0020)</button>
        <button class="preset-btn rel" onclick="applyPreset(25, 15, 4.0, 0.0, 0.0, false, 'NONE')">Bigger (> +0.0020 Lab)</button>
        <button class="preset-btn rel" onclick="applyPreset(25, 15, 6.43, 0.0, 0.0, false, 'EQUAL_UPPER')">Equal (= +0.0045)</button>
      </div>

      <div class="card-title" style="margin-top:12px;">1C. Field Anomaly & Masking Scenarios</div>
      <div class="preset-grid">
        <button class="preset-btn anom" onclick="applyPreset(25, 15, 4.5, -0.002, 0.0, false, 'MASKING_LIGHT')">1. Light Batch Masking</button>
        <button class="preset-btn anom" onclick="applyPreset(25, 20, 0.0, 0.002, 0.0005, false, 'HEAVY_GUARD')">2. Heavy Batch + Noise</button>
        <button class="preset-btn anom" style="grid-column: span 2;" onclick="applyPreset(25, 10, 0.0, 0.0, -0.0055, false, 'LOW_ANOMALY')">3. Low Anomaly (< -0.0045 Light Solvent / Cavitation)</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">2. Chamber Mixture Parameters</div>

      <div class="param-row">
        <div class="param-label"><span>Sample Temperature (°C)</span></div>
        <div class="slider-wrap">
          <input type="range" id="tempSlider" min="15" max="45" step="0.5" value="25" oninput="syncInputs('temp')">
          <input type="number" id="tempNum" value="25" step="0.5" onchange="syncInputs('tempNum')">
        </div>
      </div>

      <div class="param-row">
        <div class="param-label"><span>Ethanol Blend (%)</span></div>
        <div class="slider-wrap">
          <input type="range" id="ethSlider" min="0" max="30" step="0.5" value="20" oninput="syncInputs('eth')">
          <input type="number" id="ethNum" value="20" step="0.5" onchange="syncInputs('ethNum')">
        </div>
      </div>

      <div class="param-row">
        <div class="param-label"><span>Kerosene Adulterant (%)</span></div>
        <div class="slider-wrap">
          <input type="range" id="keroSlider" min="0" max="20" step="0.5" value="0" oninput="syncInputs('kero')">
          <input type="number" id="keroNum" value="0" step="0.5" onchange="syncInputs('keroNum')">
        </div>
      </div>

      <div class="param-row">
        <div class="param-label"><span>Refinery Batch Base Variation (Δρ₀)</span></div>
        <select id="batchSelect" onchange="clearEdgeTagAndRun()">
          <option value="-0.002">-0.002 g/cm³ (Light Refinery Batch)</option>
          <option value="-0.001">-0.001 g/cm³ (Slightly Light)</option>
          <option value="0.000" selected>0.000 g/cm³ (Nominal Reference Batch)</option>
          <option value="0.001">+0.001 g/cm³ (Slightly Heavy)</option>
          <option value="0.002">+0.002 g/cm³ (Heavy Refinery Batch)</option>
        </select>
      </div>

      <div style="margin-top:8px;">
        <label style="display:block; margin-bottom:5px; cursor:pointer;">
          <input type="checkbox" id="noiseChk" checked onchange="runEvaluation(false)"> Enable Sensor Noise (σρ = 0.0003, σε = 0.04)
        </label>
        <label style="display:block; cursor:pointer;">
          <input type="checkbox" id="pollChk" checked> Continuous 1Hz Sensor Polling
        </label>
      </div>

      <button class="btn-primary" onclick="runEvaluation(true)">+ Record Sample to SQLite</button>
    </div>

    <div class="card">
      <div class="card-title">3. Physical Reference Constants (15°C)</div>
      <table>
        <thead><tr><th>Component</th><th>Density (g/cm³)</th><th>Dielectric (ε)</th></tr></thead>
        <tbody>
          <tr><td>Base Petrol</td><td>0.740</td><td>2.10</td></tr>
          <tr><td>Ethanol (Anhydrous)</td><td>0.789</td><td>24.50</td></tr>
          <tr><td>Kerosene</td><td>0.810</td><td>2.20</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- RIGHT MAIN WORKBENCH -->
  <div>
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-label">1. TEMP-CORRECTED DENSITY</div>
        <div class="metric-val" id="mRho">0.7501</div>
        <div class="metric-sub">g/cm³ (Normalized to 15°C)</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">2. DIELECTRIC PERMITTIVITY (ε)</div>
        <div class="metric-val" id="mEps">6.57</div>
        <div class="metric-sub">Raw probe reading at chamber T</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">3. ESTIMATED ETHANOL (Ê)</div>
        <div class="metric-val" id="mEth" style="color:#0284c7;">20.0%</div>
        <div class="metric-sub">From Permittivity Inversion</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">4. DENSITY RESIDUAL (Δρ)</div>
        <div class="metric-val" id="mRes" style="color:#16a34a;">+0.0003</div>
        <div class="metric-sub">Pass: ≤0.0020 | Fail: >0.0045</div>
      </div>
    </div>

    <div id="verdictBanner" class="banner PASS">
      <div>
        <div style="font-weight:800; font-size:13px;" id="bannerTitle">RESULT: COMPLIANT FUEL BLEND (PASS)</div>
        <div style="font-size:11px; margin-top:2px;" id="bannerSub">LESSER (Δρ < +0.0020 g/cm³)</div>
      </div>
      <div class="verdict-pill" id="verdictPill">PASS</div>
    </div>

    <!-- CLEAN SYSTEM RELATIONAL & DIAGNOSTIC PANEL -->
    <div class="diag-box">
      <div style="font-weight:800; font-size:12px; color:#1e40af; margin-bottom:6px;">
        RESIDUAL RELATIONAL LOGIC (<, =, >) & SYSTEM DIAGNOSTICS
      </div>
      <div class="diag-grid">
        <div>
          <div style="font-size:11px; color:#64748b; font-weight:700;">ACTIVE RELATIONAL STATE:</div>
          <div class="op-badge" id="tOperator">LESSER (Δρ < +0.0020 g/cm³)</div>
        </div>
        <div>
          <div style="font-size:11px; color:#64748b; font-weight:700;">PHYSICAL & STATISTICAL JUSTIFICATION:</div>
          <div id="tReason" style="font-size:11.5px; color:#1e293b; margin-top:3px; line-height:1.4;">--</div>
        </div>
        <div>
          <div style="font-size:11px; color:#92400e; font-weight:700;">BOUNDARY / ANOMALY DIAGNOSTIC:</div>
          <div id="tEdge" style="font-size:11.5px; color:#78350f; margin-top:3px; line-height:1.4;">--</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
        <span style="font-weight:700; font-size:12px;">Live Two-Stage Physics Decoupling Calculation (computed by backend)</span>
        <span style="font-family:monospace; font-size:11.5px; color:#64748b;">1% Kerosene shifts Δρ by +0.0007 g/cm³</span>
      </div>
      <div class="stage-grid">
        <div>
          <strong>Stage 1: Estimate Ethanol from Dielectric (ε)</strong><br>
          <span id="st1Formula">Ê = (6.57 - 2.10) / 22.4 = 20.0%</span>
        </div>
        <div>
          <strong>Stage 2: Compute Expected Clean Density</strong><br>
          <span id="st2Formula">ρ_exp = (1 - 0.200)×0.740 + 0.200×0.789 = 0.7498</span>
        </div>
        <div>
          <strong>Stage 3: Isolate Adulteration Residual (Δρ)</strong><br>
          <span id="st3Formula">Δρ = 0.7501 - 0.7498 = +0.0003 g/cm³</span>
        </div>
      </div>
    </div>

    <div class="charts-grid">
      <div class="card">
        <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
          <span style="font-weight:700; font-size:12px;">Sensor Phase Space: Dielectric (ε) vs. Density (ρ)</span>
          <span style="font-size:11px; color:#64748b;">Dot rises above blue line when adulterated</span>
        </div>
        <canvas id="phaseChart" height="175"></canvas>
      </div>
      <div class="card">
        <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
          <span style="font-weight:700; font-size:12px;">Real-Time Residual & Tolerance Monitor (15s Window)</span>
          <span style="font-size:11px; color:#64748b;">Fixed Y-Axis (-0.006 to +0.015 g/cm³)</span>
        </div>
        <canvas id="trendChart" height="175"></canvas>
      </div>
    </div>

    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-weight:700; font-size:12px;">Persistent SQLite Inspection Log (Saved in fuel_telemetry.db)</span>
        <div>
          <span id="logCount" style="font-size:11.5px; color:#64748b; margin-right:8px;">0 samples in SQLite</span>
          <button class="btn-outline" onclick="clearLogs()">Clear DB</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Time</th><th>Node / Mode</th><th>Temp (°C)</th><th>Set Mix</th>
            <th>Measured ρ15</th><th>Measured ε</th><th>Est. Ê</th><th>Residual (Δρ)</th><th>Relational State</th><th>Verdict</th>
          </tr>
        </thead>
        <tbody id="logTableBody">
          <tr><td colspan="11" style="text-align:center; color:#94a3b8;">No samples in SQLite database. Click "+ Record Sample to SQLite".</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
let currentEdgeTag = "NONE";
let currentSolventShift = 0.0;
let residualHistory = Array(15).fill(0.0003);

const ctxPhase = document.getElementById('phaseChart').getContext('2d');
const phaseChart = new Chart(ctxPhase, {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: 'Current Fuel Sample',
        data: [{x: 6.57, y: 0.7501}],
        backgroundColor: '#16a34a',
        borderColor: '#000',
        borderWidth: 1.5,
        pointRadius: 6
      },
      {
        label: 'Compliant Legal Blend Line (E0–E30)',
        type: 'line',
        data: [{x: 2.10, y: 0.7400}, {x: 8.82, y: 0.7547}],
        borderColor: '#0284c7',
        borderDash: [5, 5],
        borderWidth: 2,
        pointRadius: 0,
        fill: false
      }
    ]
  },
  options: {
    animation: false,
    scales: {
      x: { min: 1.5, max: 10.0, title: { display: true, text: 'Dielectric ε (compensated to 15°C)' } },
      y: { min: 0.730, max: 0.770, title: { display: true, text: '15°C Compensated Density (g/cm³)' } }
    }
  }
});

const labels15s = ['-14s','-13s','-12s','-11s','-10s','-9s','-8s','-7s','-6s','-5s','-4s','-3s','-2s','-1s','-0s'];
const ctxTrend = document.getElementById('trendChart').getContext('2d');
const trendChart = new Chart(ctxTrend, {
  type: 'line',
  data: {
    labels: labels15s,
    datasets: [
      { label: 'Measured Residual (Δρ)', data: residualHistory, borderColor: '#0f172a', backgroundColor: '#0f172a', borderWidth: 2, pointRadius: 2.5, tension: 0.15 },
      { label: 'Adulteration Limit (+0.0045)', data: Array(15).fill(0.0045), borderColor: '#ef4444', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0 },
      { label: 'Abstain Band (+0.0020)', data: Array(15).fill(0.0020), borderColor: '#f59e0b', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0 },
      { label: 'Low Anomaly (-0.0045)', data: Array(15).fill(-0.0045), borderColor: '#a855f7', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0 }
    ]
  },
  options: {
    animation: false,
    scales: {
      y: { min: -0.007, max: 0.015, title: { display: true, text: 'Residual Δρ (g/cm³)' } }
    }
  }
});

function syncInputs(prefix) {
  currentEdgeTag = "NONE";
  currentSolventShift = 0.0;
  if (prefix === 'temp') document.getElementById('tempNum').value = document.getElementById('tempSlider').value;
  if (prefix === 'tempNum') document.getElementById('tempSlider').value = document.getElementById('tempNum').value;
  if (prefix === 'eth') document.getElementById('ethNum').value = document.getElementById('ethSlider').value;
  if (prefix === 'ethNum') document.getElementById('ethSlider').value = document.getElementById('ethNum').value;
  if (prefix === 'kero') document.getElementById('keroNum').value = document.getElementById('keroSlider').value;
  if (prefix === 'keroNum') document.getElementById('keroSlider').value = document.getElementById('keroNum').value;
  runEvaluation(false);
}

function clearEdgeTagAndRun() {
  currentEdgeTag = "NONE";
  currentSolventShift = 0.0;
  runEvaluation(false);
}

function applyPreset(temp, eth, kero, batch, solvent, noise, tag) {
  document.getElementById('tempSlider').value = temp;
  document.getElementById('tempNum').value = temp;
  document.getElementById('ethSlider').value = eth;
  document.getElementById('ethNum').value = eth;
  document.getElementById('keroSlider').value = kero;
  document.getElementById('keroNum').value = kero;
  document.getElementById('batchSelect').value = batch.toFixed(3);
  document.getElementById('noiseChk').checked = noise;
  currentEdgeTag = tag;
  currentSolventShift = solvent;
  runEvaluation(false);
}

async function runEvaluation(recordToDb) {
  const payload = {
    temp_c: parseFloat(document.getElementById('tempNum').value),
    ethanol_pct: parseFloat(document.getElementById('ethNum').value),
    kerosene_pct: parseFloat(document.getElementById('keroNum').value),
    batch_offset: parseFloat(document.getElementById('batchSelect').value),
    solvent_shift: currentSolventShift,
    noise_enabled: document.getElementById('noiseChk').checked,
    edge_case_tag: currentEdgeTag,
    node_id: "ESP32-PUN-084",
    record_to_db: recordToDb
  };

  const res = await fetch('/api/telemetry/evaluate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await res.json();

  document.getElementById('seqBadge').innerText = `IOT EDGE: ESP32-PUN-084 [SEQ #${d.seq}]`;
  document.getElementById('clockSpan').innerText = d.timestamp;
  document.getElementById('mRho').innerText = d.rho_comp15.toFixed(4);
  document.getElementById('mEps').innerText = d.eps_obs.toFixed(2);
  
  // Ethanol text color updates based on severity
  const mEth = document.getElementById('mEth');
  mEth.innerText = d.est_e_pct.toFixed(1) + '%';
  if (d.est_e_pct > 25.0) { mEth.style.color = '#dc2626'; }
  else if (d.est_e_pct > 20.5) { mEth.style.color = '#d97706'; }
  else { mEth.style.color = '#0284c7'; }

  const sign = d.delta_rho >= 0 ? '+' : '';
  const mRes = document.getElementById('mRes');
  mRes.innerText = sign + d.delta_rho.toFixed(4);

  // Syncing Banner and Dot colors using the backend UI class
  const banner = document.getElementById('verdictBanner');
  banner.className = 'banner ' + d.ui_class;
  let dotColor = '#16a34a';
  
  if (d.ui_class === 'PASS') {
    mRes.style.color = '#16a34a';
    dotColor = '#16a34a';
  } else if (d.ui_class === 'ABSTAIN') {
    mRes.style.color = '#d97706';
    dotColor = '#d97706';
  } else if (d.ui_class === 'LOW') {
    mRes.style.color = '#7e22ce';
    dotColor = '#7e22ce';
  } else {
    mRes.style.color = '#dc2626';
    dotColor = '#dc2626';
  }

  document.getElementById('bannerTitle').innerText = d.banner_text;
  document.getElementById('bannerSub').innerText = d.rel_operator;
  document.getElementById('verdictPill').innerText = d.verdict;

  document.getElementById('tOperator').innerText = d.rel_operator;
  document.getElementById('tReason').innerText = d.valid_reason;
  document.getElementById('tEdge').innerText = d.edge_explanation;

  document.getElementById('st1Formula').innerText = `Ê = (${d.eps_obs.toFixed(2)} - 2.10) / 22.4 = ${d.est_e_pct.toFixed(1)}%`;
  document.getElementById('st2Formula').innerText = `ρ_exp = (1 - ${d.est_e_frac.toFixed(3)})×0.740 + ${d.est_e_frac.toFixed(3)}×0.789 = ${d.rho_exp.toFixed(4)}`;
  document.getElementById('st3Formula').innerText = `Δρ = ${d.rho_comp15.toFixed(4)} - ${d.rho_exp.toFixed(4)} = ${sign}${d.delta_rho.toFixed(4)} g/cm³`;

  phaseChart.data.datasets[0].data = [{x: d.eps_obs, y: d.rho_comp15}];
  phaseChart.data.datasets[0].backgroundColor = dotColor;
  phaseChart.update();

  residualHistory.shift();
  residualHistory.push(d.delta_rho);
  trendChart.update();

  if (recordToDb) loadLogs();
}

async function loadLogs() {
  const res = await fetch('/api/logs');
  const data = await res.json();
  const tbody = document.getElementById('logTableBody');
  document.getElementById('logCount').innerText = `${data.logs.length} samples in SQLite`;
  if (data.logs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align:center; color:#94a3b8;">No samples in SQLite database. Click "+ Record Sample to SQLite".</td></tr>';
    return;
  }
  tbody.innerHTML = data.logs.map(r => {
    const sign = r[8] >= 0 ? '+' : '';
    // Parsing text string to dynamically match log row color with the exact issue
    let rowColor = '#166534';
    if (r[10].includes('FAIL') || r[10].includes('ADULTERATED')) rowColor = '#991b1b';
    else if (r[10].includes('WARN') || r[10].includes('ABSTAIN')) rowColor = '#92400e';
    else if (r[10].includes('LOW')) rowColor = '#6b21a8';
    
    return `<tr>
      <td>#${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3].toFixed(1)}</td><td>${r[4]}</td>
      <td>${r[5].toFixed(4)}</td><td>${r[6].toFixed(2)}</td><td>${r[7].toFixed(1)}%</td>
      <td style="font-weight:700; color:${rowColor};">${sign}${r[8].toFixed(4)}</td>
      <td style="font-family:monospace; font-size:11px;">${r[9]}</td>
      <td style="font-weight:700; color:${rowColor};">${r[10]}</td>
    </tr>`;
  }).join('');
}

async function clearLogs() {
  await fetch('/api/logs/clear', {method: 'POST'});
  loadLogs();
}

setInterval(() => {
  if (document.getElementById('pollChk').checked) {
    runEvaluation(false);
  }
}, 1000);

loadLogs();
runEvaluation(false);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)