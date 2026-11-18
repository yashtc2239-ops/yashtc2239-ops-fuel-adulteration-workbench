# IS 17021 Fuel Adulteration & Ethanol Decoupling Workbench

This project is an IoT sensor simulation and two-stage residual verification system designed to detect illegal kerosene adulteration in E20 petrol blends, adhering to the IS 17021:2026 standard. 

**Team Members:**
* Aditi Wankhade (47)
* Tejaswini Wankhede (48)
* Parth Sawant (58)
* Yash Chavan (61)

## The Core Problem
Traditional fuel purity tests rely on density. However, legal Ethanol ($0.789 \text{ g/cm}^3$) and illegal Kerosene ($0.810 \text{ g/cm}^3$) are both heavier than Base Petrol ($0.740 \text{ g/cm}^3$). A standard density sensor cannot determine whether a weight increase is due to legal ethanol blending or illegal kerosene adulteration. 

## The Solution: Two-Stage Physics Decoupling
We utilize orthogonal sensing to break this ambiguity using two physical properties: **Dielectric Permittivity (ε)** and **Temperature-Compensated Density (ρ)**.

1. **Stage 1 (Isolate Ethanol):** Kerosene and Petrol have almost identical dielectric constants (~2.10 to 2.20), making Kerosene virtually invisible to the capacitive RF sensor. Ethanol has a massive dielectric constant (24.50). The system isolates the exact Ethanol percentage using dielectric inversion.
2. **Stage 2 (Expected Density):** The backend mathematically computes the expected clean density of the fuel based *only* on the detected ethanol percentage.
3. **Stage 3 (Residual Verification):** The actual chamber density is measured and compared against the expected clean density. The resulting **Density Residual (Δρ)** isolates the heavy-hydrocarbon (kerosene) adulteration. 

## System Architecture
* **`server.py`:** A FastAPI backend running Looyenga’s Mixing Rule, $15^\circ\text{C}$ thermal compensation, and Gaussian sensor noise injection. It hosts the interactive web dashboard with real-time relational boundary diagnostics (`<`, `=`, `>`) and edge-case masking simulations.
* **`edge_node.py`:** Simulates an `ESP32` IoT edge node polling sensor readings via HTTP REST and logging anomalies.
* **SQLite Database:** Persistent inspection logging with CSV export capabilities for regulatory compliance.

## How to Run Locally

1. **Install Dependencies:**
   ```bash
   pip install fastapi uvicorn pydantic requests
