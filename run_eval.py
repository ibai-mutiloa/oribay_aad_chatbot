import json
import math
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_URL = "https://chatbot-aad-809725501359.europe-west1.run.app"


def identity_token() -> str:
    completed = subprocess.run(
        ["gcloud", "auth", "print-identity-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def post_message(url: str, token: str, text: str, scenario: str) -> tuple[str, float]:
    payload = {
        "chat": {
            "messagePayload": {
                "message": {
                    "text": text,
                    "space": {"name": f"spaces/eval-{scenario}"},
                    "sender": {"name": "users/evaluator"},
                }
            }
        }
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    answer = (
        body.get("hostAppDataAction", {})
        .get("chatDataAction", {})
        .get("createMessageAction", {})
        .get("message", {})
        .get("text")
        or body.get("text")
        or ""
    )
    return answer, elapsed


def percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def post_with_refresh(url: str, token: str, text: str, scenario: str) -> tuple[str, float, str]:
    """Retry once with a fresh identity token when Cloud Run returns 401."""
    try:
        answer, elapsed = post_message(url, token, text, scenario)
        return answer, elapsed, token
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        refreshed = identity_token()
        answer, elapsed = post_message(url, refreshed, text, scenario)
        return answer, elapsed, refreshed


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    cases_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name("eval_cases.json")
    scenarios = json.loads(cases_path.read_text(encoding="utf-8"))
    token = identity_token()
    results = []

    for scenario in scenarios:
        scenario_name = scenario["scenario"]
        _, _, token = post_with_refresh(url, token, "reiniciar memoria", scenario_name)
        for step_number, step in enumerate(scenario["steps"], start=1):
            try:
                answer, elapsed, token = post_with_refresh(
                    url, token, step["prompt"], scenario_name
                )
                missing = [
                    pattern
                    for pattern in step["must_match"]
                    if not re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
                ]
                forbidden = [
                    pattern
                    for pattern in step.get("must_not_match", [])
                    if re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
                ]
                accurate = not missing and not forbidden
                fast = elapsed <= step["max_seconds"]
                passed = accurate and fast
                results.append({"passed": passed, "elapsed": elapsed})
                status = "PASS" if passed else "FAIL"
                print(f"[{status}] {scenario_name} #{step_number} ({elapsed:.2f}s)")
                if missing:
                    print(f"  Faltan patrones: {missing}")
                if forbidden:
                    print(f"  Patrones prohibidos encontrados: {forbidden}")
                if not fast:
                    print(f"  Supera {step['max_seconds']}s")
                print(f"  Pregunta: {step['prompt']}")
                print(f"  Respuesta: {answer}\n")
            except (
                urllib.error.URLError,
                TimeoutError,
                subprocess.SubprocessError,
                ValueError,
            ) as error:
                results.append({"passed": False, "elapsed": 60.0})
                print(f"[ERROR] {scenario_name} #{step_number}: {error}\n")

    latencies = [result["elapsed"] for result in results]
    passed = sum(1 for result in results if result["passed"])
    total = len(results)
    print("=== RESUMEN ===")
    print(f"Exactitud/rendimiento: {passed}/{total} ({passed / total * 100:.1f}%)")
    print(f"Latencia media: {statistics.mean(latencies):.2f}s")
    print(f"Latencia p95: {percentile_95(latencies):.2f}s")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
