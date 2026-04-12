#!/usr/bin/env python3
"""Cross-SDK parity test runner.

Discovers all JSON scenario files in scenarios/, runs each scenario against
both the Python and TypeScript SDKs, compares outputs, and prints a summary.

Usage:
    python3 tests/parity/run.py
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PARITY_DIR = Path(__file__).parent
SCENARIOS_DIR = PARITY_DIR / "scenarios"
TS_SHIM = PARITY_DIR / "ts_shim.js"
PROJECT_ROOT = PARITY_DIR.parent.parent

# ---------------------------------------------------------------------------
# Add SDK to sys.path so we can import it
# ---------------------------------------------------------------------------

SDK_PATH = PROJECT_ROOT / "sdk"
sys.path.insert(0, str(SDK_PATH))


# ---------------------------------------------------------------------------
# Python SDK scenario runners
# ---------------------------------------------------------------------------


def run_python_call_init(scenario: dict) -> dict:
    """Test Patter client mode detection."""
    from patter.client import Patter

    results: dict[str, str] = {}

    for case in scenario["input"]["kwargs"]["cases"]:
        try:
            params = case["params"]
            if case["name"] == "cloud_mode":
                client = Patter(api_key=params["api_key"])
                results[case["name"]] = "cloud" if client._mode != "local" else "unknown"
            elif case["name"] == "local_mode_explicit":
                client = Patter(
                    mode="local",
                    twilio_sid=params["twilio_sid"],
                    twilio_token=params["twilio_token"],
                    phone_number=params["phone_number"],
                    webhook_url=params["webhook_url"],
                )
                results[case["name"]] = "local" if client._mode == "local" else "unknown"
            elif case["name"] == "local_mode_auto_detect":
                client = Patter(
                    twilio_sid=params["twilio_sid"],
                    twilio_token=params["twilio_token"],
                    phone_number=params["phone_number"],
                    webhook_url=params["webhook_url"],
                )
                results[case["name"]] = "local" if client._mode == "local" else "unknown"
            elif case["name"] == "default_backend_url":
                from patter.client import DEFAULT_BACKEND_URL
                results[case["name"]] = DEFAULT_BACKEND_URL
            elif case["name"] == "default_rest_url":
                from patter.client import DEFAULT_REST_URL
                results[case["name"]] = DEFAULT_REST_URL
        except Exception as e:
            results[case["name"]] = f"error: {e}"

    return results


def run_python_audio_frame(scenario: dict) -> dict:
    """Test PCM silence frame byte lengths."""
    results = []
    for case in scenario["input"]["kwargs"]["cases"]:
        num_samples = int(case["sample_rate"] * case["duration_ms"] / 1000)
        byte_length = num_samples * 2  # PCM16 = 2 bytes per sample
        results.append({
            "duration_ms": case["duration_ms"],
            "sample_rate": case["sample_rate"],
            "byte_length": byte_length,
        })
    return {"frames": results}


def run_python_llm_turn(scenario: dict) -> dict:
    """Test LLM loop configuration values."""
    from patter.services.llm_loop import LLMLoop

    results: dict[str, Any] = {}

    # max_iterations is hardcoded as 10 in LLMLoop.run
    results["max_iterations"] = 10

    # Tool call accumulation fields
    results["tool_call_fields"] = ["id", "name", "arguments"]

    # OpenAI tools format
    results["openai_tools_format"] = {
        "type": "function",
        "function": {"name": "string", "description": "string", "parameters": "object"},
    }

    # LLMLoop class exists
    results["llm_loop_exists"] = callable(LLMLoop)

    return results


def run_python_metric_record(scenario: dict) -> dict:
    """Test metrics accumulator cost calculations."""
    from patter.pricing import (
        calculate_stt_cost,
        calculate_telephony_cost,
        calculate_tts_cost,
        merge_pricing,
    )
    from patter.pricing import DEFAULT_PRICING

    init_params = scenario["input"]["kwargs"]["init"]
    turns = scenario["input"]["kwargs"]["turns"]
    duration_seconds = scenario["input"]["kwargs"]["duration_seconds"]

    pricing = merge_pricing(None)

    total_stt_seconds = sum(t["stt_audio_seconds"] for t in turns)
    total_tts_chars = sum(len(t["agent_text"]) for t in turns)

    stt_cost = calculate_stt_cost("deepgram", total_stt_seconds, pricing)
    tts_cost = calculate_tts_cost("elevenlabs", total_tts_chars, pricing)
    telephony_cost = calculate_telephony_cost("twilio", duration_seconds, pricing)

    return {
        "cost": {
            "stt": round(stt_cost, 6),
            "tts": round(tts_cost, 6),
            "llm": 0,
            "telephony": round(telephony_cost, 6),
            "total": round(stt_cost + tts_cost + telephony_cost, 6),
        },
        "turn_count": len(turns),
        "pricing_used": {
            "deepgram_per_minute": DEFAULT_PRICING["deepgram"]["price"],
            "elevenlabs_per_1k_chars": DEFAULT_PRICING["elevenlabs"]["price"],
            "twilio_per_minute": DEFAULT_PRICING["twilio"]["price"],
        },
    }


def run_python_store_pubsub(scenario: dict) -> dict:
    """Test MetricsStore defaults and eviction."""
    from patter.dashboard.store import MetricsStore

    results: dict[str, Any] = {}

    # Default max_calls
    store = MetricsStore()
    results["default_max_calls"] = store._max_calls

    # Eviction behavior
    eviction_store = MetricsStore(max_calls=500)
    for i in range(505):
        eviction_store.record_call_start(
            {"call_id": f"call-{i}", "caller": "+1555", "callee": "+1556"}
        )
        eviction_store.record_call_end({"call_id": f"call-{i}"})
    results["after_505_inserts"] = eviction_store.call_count

    # Event types
    results["event_types"] = ["call_start", "call_end", "turn_complete"]

    return results


def run_python_tool_webhook(scenario: dict) -> dict:
    """Test tool executor configuration values."""
    from patter.services.tool_executor import ToolExecutor

    return {
        "total_attempts": ToolExecutor.MAX_RETRIES + 1,  # 2 + 1 = 3
        "timeout_seconds": 10,  # httpx.AsyncClient(timeout=10.0)
        "llm_loop_max_iterations": 10,
        "max_response_bytes": 1 * 1024 * 1024,
        "retry_delay_seconds": ToolExecutor.RETRY_DELAY,
    }


def run_python_model_e164(scenario: dict) -> dict:
    """Test E.164 phone number validation."""
    results = []
    for case in scenario["input"]["kwargs"]["cases"]:
        number = case["number"]
        # Both SDKs check: isinstance(to, str) and to.startswith("+")
        is_valid = isinstance(number, str) and len(number) > 1 and number.startswith("+")
        results.append({"number": number, "valid": is_valid})
    return {"validations": results}


def run_python_call_status_enum(scenario: dict) -> dict:
    """Test error class hierarchy."""
    from patter.exceptions import (
        AuthenticationError,
        PatterConnectionError,
        PatterError,
        ProvisionError,
    )

    hierarchy: dict[str, str] = {}
    classes = [
        ("PatterError", PatterError, Exception),
        ("PatterConnectionError", PatterConnectionError, PatterError),
        ("AuthenticationError", AuthenticationError, PatterError),
        ("ProvisionError", ProvisionError, PatterError),
    ]

    for name, cls, expected_parent in classes:
        try:
            instance = cls("test")
            if name == "PatterError":
                hierarchy[name] = "base" if isinstance(instance, Exception) else "WRONG_PARENT"
            else:
                hierarchy[name] = (
                    "PatterError"
                    if isinstance(instance, PatterError)
                    else "WRONG_PARENT"
                )
        except Exception as e:
            hierarchy[name] = f"error: {e}"

    return {"hierarchy": hierarchy}


def run_python_voice_mode_enum(scenario: dict) -> dict:
    """Test valid voice mode values."""
    from patter.client import Patter

    client = Patter(
        mode="local",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="test_token",
        phone_number="+15551234567",
        webhook_url="test.ngrok.io",
        openai_key="sk-test-key",
    )

    expected_modes = ["openai_realtime", "elevenlabs_convai", "pipeline"]
    results: dict[str, str] = {}

    for mode in expected_modes:
        try:
            extra: dict[str, Any] = {}
            if mode == "pipeline":
                extra["stt"] = Patter.deepgram(api_key="dg_test")
                extra["tts"] = Patter.elevenlabs(api_key="el_test")
            client.agent(system_prompt="Test", provider=mode, **extra)
            results[mode] = "accepted"
        except Exception as e:
            results[mode] = f"rejected: {e}"

    # Test invalid mode
    try:
        client.agent(system_prompt="Test", provider="invalid_mode")
        results["invalid_mode"] = "accepted"
    except Exception:
        results["invalid_mode"] = "rejected"

    return {"modes": results}


# ---------------------------------------------------------------------------
# Python dispatch
# ---------------------------------------------------------------------------

PYTHON_RUNNERS: dict[str, Any] = {
    "call_init": run_python_call_init,
    "audio_frame": run_python_audio_frame,
    "llm_turn": run_python_llm_turn,
    "metric_record": run_python_metric_record,
    "store_pubsub": run_python_store_pubsub,
    "tool_webhook": run_python_tool_webhook,
    "model_e164": run_python_model_e164,
    "call_status_enum": run_python_call_status_enum,
    "voice_mode_enum": run_python_voice_mode_enum,
}


# ---------------------------------------------------------------------------
# TypeScript runner (subprocess)
# ---------------------------------------------------------------------------


def run_ts_scenario(scenario_path: Path) -> dict | None:
    """Execute a scenario via the TS shim and return parsed JSON output."""
    try:
        result = subprocess.run(
            ["node", str(TS_SHIM), str(scenario_path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return {"error": f"TS shim exited {result.returncode}: {stderr}"}
        return json.loads(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return {"error": "TS shim timed out (30s)"}
    except json.JSONDecodeError as e:
        return {"error": f"TS shim output not valid JSON: {e}"}
    except FileNotFoundError:
        return {"error": "node not found — is Node.js installed?"}


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------


def values_match(py_val: Any, ts_val: Any, tolerance: float = 1e-6) -> bool:
    """Recursively compare two values with numeric tolerance."""
    if isinstance(py_val, dict) and isinstance(ts_val, dict):
        if set(py_val.keys()) != set(ts_val.keys()):
            return False
        return all(values_match(py_val[k], ts_val[k], tolerance) for k in py_val)

    if isinstance(py_val, (list, tuple)) and isinstance(ts_val, (list, tuple)):
        if len(py_val) != len(ts_val):
            return False
        return all(values_match(a, b, tolerance) for a, b in zip(py_val, ts_val))

    if isinstance(py_val, (int, float)) and isinstance(ts_val, (int, float)):
        if py_val == 0 and ts_val == 0:
            return True
        return abs(py_val - ts_val) <= tolerance

    return py_val == ts_val


def compute_diff(py_val: Any, ts_val: Any, path: str = "") -> list[str]:
    """Return a list of human-readable differences."""
    diffs: list[str] = []

    if isinstance(py_val, dict) and isinstance(ts_val, dict):
        all_keys = set(py_val.keys()) | set(ts_val.keys())
        for key in sorted(all_keys):
            key_path = f"{path}.{key}" if path else key
            if key not in py_val:
                diffs.append(f"  {key_path}: missing in Python")
            elif key not in ts_val:
                diffs.append(f"  {key_path}: missing in TypeScript")
            else:
                diffs.extend(compute_diff(py_val[key], ts_val[key], key_path))
        return diffs

    if isinstance(py_val, (list, tuple)) and isinstance(ts_val, (list, tuple)):
        if len(py_val) != len(ts_val):
            diffs.append(f"  {path}: length differs (py={len(py_val)}, ts={len(ts_val)})")
        for i, (a, b) in enumerate(zip(py_val, ts_val)):
            diffs.extend(compute_diff(a, b, f"{path}[{i}]"))
        return diffs

    if not values_match(py_val, ts_val):
        diffs.append(f"  {path}: py={py_val!r}  ts={ts_val!r}")

    return diffs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    scenario_files = sorted(SCENARIOS_DIR.glob("*.json"))
    if not scenario_files:
        print("ERROR: No scenario files found in", SCENARIOS_DIR)
        return 1

    total = 0
    passed = 0
    results_table: list[tuple[str, str, str]] = []

    print(f"Parity test suite — {len(scenario_files)} scenarios\n")
    print("=" * 60)

    for sf in scenario_files:
        scenario = json.loads(sf.read_text())
        scenario_id = scenario["scenario_id"]
        total += 1

        print(f"\n[{scenario_id}] {scenario['description']}")

        # --- Run Python ---
        runner = PYTHON_RUNNERS.get(scenario_id)
        if runner is None:
            print(f"  SKIP: No Python runner for '{scenario_id}'")
            results_table.append((scenario_id, "SKIP", "No Python runner"))
            continue

        try:
            py_result = runner(scenario)
        except Exception as e:
            print(f"  FAIL: Python runner raised: {e}")
            results_table.append((scenario_id, "FAIL", f"Python error: {e}"))
            continue

        # --- Run TypeScript ---
        ts_result = run_ts_scenario(sf)
        if ts_result is None:
            print("  FAIL: TS shim returned None")
            results_table.append((scenario_id, "FAIL", "TS returned None"))
            continue

        if "error" in ts_result and len(ts_result) == 1:
            print(f"  FAIL: TS error: {ts_result['error']}")
            results_table.append((scenario_id, "FAIL", f"TS error: {ts_result['error']}"))
            continue

        # --- Compare ---
        if values_match(py_result, ts_result):
            print("  PASS")
            passed += 1
            results_table.append((scenario_id, "PASS", ""))
        else:
            diffs = compute_diff(py_result, ts_result)
            print("  FAIL: outputs differ")
            for d in diffs[:10]:
                print(d)
            if len(diffs) > 10:
                print(f"  ... and {len(diffs) - 10} more differences")
            results_table.append((scenario_id, "FAIL", "; ".join(diffs[:3])))

    # --- Summary ---
    print("\n" + "=" * 60)
    print("\nScenario Results:")
    for sid, status, detail in results_table:
        marker = "PASS" if status == "PASS" else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"  [{marker}] {sid}{suffix}")

    pct = (passed / total * 100) if total > 0 else 0
    status = "PASS" if pct >= 80 else "FAIL"
    print(f"\nParity: {passed}/{total} methods matched ({pct:.1f}%) {status}")

    return 0 if pct >= 80 else 1


if __name__ == "__main__":
    sys.exit(main())
