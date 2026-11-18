import time
import argparse
import requests

SERVER_URL = "http://127.0.0.1:8000"


def run_edge_node(auto_log_alarms: bool):
    print(f"[ESP32-PUN-084] Starting 1Hz Telemetry Node -> {SERVER_URL}")
    last_logged_state = None

    while True:
        try:
            sp_res = requests.get(f"{SERVER_URL}/api/edge/setpoint", timeout=2)
            if sp_res.status_code == 200:
                sp = sp_res.json()
                state_key = (sp["ethanol_pct"], sp["kerosene_pct"], sp["edge_case_tag"])

                payload = {
                    "temp_c": sp["temp_c"],
                    "ethanol_pct": sp["ethanol_pct"],
                    "kerosene_pct": sp["kerosene_pct"],
                    "batch_offset": sp["batch_offset"],
                    "solvent_shift": sp.get("solvent_shift", 0.0),
                    "noise_enabled": sp["noise_enabled"],
                    "edge_case_tag": sp.get("edge_case_tag", "NONE"),
                    "node_id": "ESP32-PUN-084-AUTO",
                    "record_to_db": False
                }

                # Eval first to check if alarm state triggered
                eval_res = requests.post(f"{SERVER_URL}/api/edge/ingest", json=payload, timeout=2)
                if eval_res.status_code == 200:
                    verdict = eval_res.json().get("verdict", "PASS")
                    if auto_log_alarms and verdict in ("ADULTERATED", "LOW ANOMALY") and state_key != last_logged_state:
                        payload["record_to_db"] = True
                        requests.post(f"{SERVER_URL}/api/telemetry/evaluate", json=payload, timeout=2)
                        last_logged_state = state_key
                        print(f"[ALARM LOGGED] Mix E{sp['ethanol_pct']}%/K{sp['kerosene_pct']}% -> {verdict}")
        except Exception as e:
            print(f"[WAITING FOR SERVER] {e}")

        time.sleep(1.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-log-alarms", action="store_true", help="Automatically log adulterated samples to SQLite")
    args = parser.parse_args()
    run_edge_node(args.auto_log_alarms)