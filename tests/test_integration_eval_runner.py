from argparse import Namespace
from types import SimpleNamespace
import json
import pytest

import integration_eval_v83 as runner


def valid_args(**overrides):
    values = {
        "url": "https://eval-v83-test---aad-eval-v83-abc.a.run.app",
        "expected_memory_collection": "chatbot_aad_eval_localtest",
        "expected_firestore_database": "aad-eval-v83",
        "gcp_project": "aad-staging-project",
        "location": "europe-west1",
        "cloud_run_service": "aad-eval-v83",
        "revision": "aad-eval-v83-00001-abc",
        "revision_tag": "eval-v83-test",
        "week_current": "2026-37",
        "calendar_start": "2026-09-07",
        "machine_a": "RB7",
        "machine_b": "RB8",
        "article_a": "AAAA000001",
        "article_b": "BBBB000002",
    }
    values.update(overrides)
    return Namespace(**values)


def test_explicit_tagged_cloud_run_url_is_accepted():
    assert runner.validate_target_url(valid_args().url).startswith("https://eval-v83-test---")


@pytest.mark.parametrize("url", [
    "https://chatbot-aad-809725501359.europe-west1.run.app",
    "https://aad-eval-v83-abc.a.run.app",
    "http://eval---aad-v83-abc.a.run.app",
])
def test_production_or_untagged_targets_are_rejected(url):
    with pytest.raises(ValueError):
        runner.validate_target_url(url)


def test_parser_requires_explicit_url_and_execution_mode():
    parser = runner.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def _forbid_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No debe invocarse ningún servicio externo en esta prueba.")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    monkeypatch.setattr(runner.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(runner, "verify_isolated_deployment", forbidden)
    monkeypatch.setattr(runner, "identity_token", forbidden)


def test_validate_only_runs_manifest_checks_without_parameters_or_services(monkeypatch):
    _forbid_external_calls(monkeypatch)
    result = runner.main(["--validate-only"])
    assert result == 0


def test_execute_real_rejects_missing_parameters_before_external_calls(monkeypatch, capsys):
    _forbid_external_calls(monkeypatch)
    with pytest.raises(SystemExit) as error:
        runner.main(["--execute-real"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "--execute-real requiere estos argumentos" in stderr
    assert "--url" in stderr and "--article-b" in stderr
    assert "--expected-firestore-database" in stderr


def test_malformed_scenario_manifest_fails_local_validation(tmp_path, monkeypatch, capsys):
    _forbid_external_calls(monkeypatch)
    malformed = json.loads(runner.DEFAULT_CASES.read_text(encoding="utf-8"))
    malformed[0]["id"] = "Invalid ID"
    cases = tmp_path / "malformed.json"
    cases.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        runner.main(["--validate-only", "--cases", str(cases)])
    assert error.value.code == 2
    assert "id único y seguro" in capsys.readouterr().err


def test_scenario_missing_required_prompt_field_fails_local_validation(tmp_path, monkeypatch, capsys):
    _forbid_external_calls(monkeypatch)
    malformed = json.loads(runner.DEFAULT_CASES.read_text(encoding="utf-8"))
    del malformed[0]["steps"][0]["prompt"]
    cases = tmp_path / "missing-prompt.json"
    cases.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        runner.main(["--validate-only", "--cases", str(cases)])
    assert error.value.code == 2
    assert "falta una pregunta prompt" in capsys.readouterr().err


def test_collection_must_be_separate_from_production():
    runner.validate_collection("chatbot_aad_eval_run123")
    with pytest.raises(ValueError):
        runner.validate_collection("chatbot_aad_conversations")


def test_synthetic_chat_space_suppresses_progress_message_api_calls():
    assert runner.evaluation_space_name("run123", "oee_case").startswith("spaces/eval-")


def test_iso_week_rollover_is_resolved_without_hardcoded_values():
    assert runner.previous_iso_week("2026-01") == "2025-52"


def test_numeric_parser_understands_spanish_number_formats():
    assert runner.numeric_value("Disponibilidad 1.234,56 %", r"([0-9.,]+)") == 1234.56
    assert runner.numeric_value("Disponibilidad 87,25 %", r"([0-9.,]+)") == 87.25
    assert runner.numeric_value("Producción 1.234 piezas", r"([0-9.,]+)") == 1234


def test_scenario_manifest_has_16_multi_turn_conversations_and_resolves_parameters():
    values = runner.validate_arguments(valid_args())
    scenarios = runner.load_scenarios(runner.DEFAULT_CASES, values)
    assert len(scenarios) == 16
    assert all(len(scenario["steps"]) >= 2 for scenario in scenarios)
    assert all("{week_current}" not in step["prompt"] for scenario in scenarios for step in scenario["steps"])


def test_sql_assertion_supports_positive_and_negative_filters(monkeypatch):
    monkeypatch.setattr(runner, "run_reference_query", lambda *_args: [])
    step = {
        "sql_checks": [{"name": "expected machine", "pattern": "RB7", "not_pattern": "RB8"}],
    }
    good = runner.evaluate_step(step, "answer", "WHERE maquina = 'RB7'", "p", "europe-west1")
    bad = runner.evaluate_step(step, "answer", "WHERE maquina IN ('RB7','RB8')", "p", "europe-west1")
    assert good["categories"]["sql"] == "PASS"
    assert bad["categories"]["sql"] == "FAIL"


def test_missing_sql_trace_blocks_filter_verification():
    result = runner.evaluate_step(
        {"sql_checks": [{"name": "week", "pattern": "semana"}]},
        "answer", None, "p", "europe-west1",
    )
    assert result["categories"]["sql"] == "BLOCKED"
    assert result["status"] == "BLOCKED"


def test_preflight_requires_isolated_collection_and_zero_traffic(monkeypatch):
    results = [
        {"spec": {"containers": [{"env": [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_eval_test"}, {"name": "FIRESTORE_DATABASE", "value": "aad-eval-v83"}]}]}},
        {"status": {"traffic": [{"revisionName": "aad-eval-v83-00001-abc", "tag": "eval-v83-test", "percent": 0}]}},
    ]

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=json.dumps(results.pop(0)))

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.verify_isolated_deployment(
        "aad-staging-project", "europe-west1", "aad-eval-v83",
        "aad-eval-v83-00001-abc", "eval-v83-test", "chatbot_aad_eval_test", "aad-eval-v83",
    )

    results.extend([
        {"spec": {"containers": [{"env": [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_conversations"}, {"name": "FIRESTORE_DATABASE", "value": "aad-eval-v83"}]}]}},
    ])
    with pytest.raises(ValueError):
        runner.verify_isolated_deployment(
            "aad-staging-project", "europe-west1", "aad-eval-v83",
            "aad-eval-v83-00001-abc", "eval-v83-test", "chatbot_aad_eval_test",
            "aad-eval-v83",
        )
    results.extend([
        {"spec": {"containers": [{"env": [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_eval_test"}, {"name": "FIRESTORE_DATABASE", "value": "aad-eval-v83"}]}]}},
        {"status": {"traffic": [{"revisionName": "aad-eval-v83-00001-abc", "tag": "eval-v83-test", "percent": 100}]}},
    ])
    with pytest.raises(ValueError):
        runner.verify_isolated_deployment(
            "aad-staging-project", "europe-west1", "aad-eval-v83",
            "aad-eval-v83-00001-abc", "eval-v83-test", "chatbot_aad_eval_test",
            "aad-eval-v83",
        )


@pytest.mark.parametrize("env_entries", [
    [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_eval_test"}],
    [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_eval_test"},
     {"name": "FIRESTORE_DATABASE", "value": "wrong-eval-db"}],
    [{"name": "MEMORY_COLLECTION", "value": "chatbot_aad_eval_test"},
     {"name": "FIRESTORE_DATABASE", "value": "(default)"}],
])
def test_preflight_rejects_missing_or_wrong_firestore_database(monkeypatch, env_entries):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps({"spec": {"containers": [{"env": env_entries}]}})
        ),
    )
    with pytest.raises(ValueError, match="base Firestore"):
        runner.verify_isolated_deployment(
            "aad-staging-project", "europe-west1", "aad-eval-v83",
            "aad-eval-v83-00001-abc", "eval-v83-test", "chatbot_aad_eval_test",
            "aad-eval-v83",
        )


@pytest.mark.parametrize("database", ["", "(default)"])
def test_execute_arguments_reject_empty_or_default_firestore_database(database):
    with pytest.raises(ValueError, match="expected-firestore-database"):
        runner.validate_arguments(valid_args(expected_firestore_database=database))


def test_evaluation_firestore_client_uses_expected_database(monkeypatch):
    from google.cloud import firestore

    calls = []
    monkeypatch.setattr(
        firestore, "Client", lambda **kwargs: calls.append(kwargs) or object()
    )
    client = runner.create_firestore_client("aad-staging-project", "aad-eval-v83")
    assert client is not None
    assert calls == [{"project": "aad-staging-project", "database": "aad-eval-v83"}]


def test_execute_argument_validation_still_requires_dedicated_service_and_matching_tag():
    with pytest.raises(ValueError, match="dedicado a evaluación"):
        runner.validate_arguments(valid_args(cloud_run_service="chat-prod"))
    with pytest.raises(ValueError, match="tag y servicio"):
        runner.validate_arguments(valid_args(url="https://eval-v83-test---another-service-abc.a.run.app"))


def test_firestore_verifier_checks_last_saved_user_and_assistant_turn():
    class Snapshot:
        exists = True

        def to_dict(self):
            return {"turns": [{"user": "pregunta", "assistant": "respuesta"}]}

    class Document:
        def get(self):
            return Snapshot()

    class Collection:
        def document(self, _document_id):
            return Document()

    class FakeFirestore:
        def collection(self, _collection):
            return Collection()

    assert runner.verify_firestore_turn(FakeFirestore(), "chatbot_aad_eval_test", "doc", "pregunta", "respuesta")
    assert not runner.verify_firestore_turn(FakeFirestore(), "chatbot_aad_eval_test", "doc", "otra", "respuesta")

    class MarkdownSnapshot(Snapshot):
        def to_dict(self):
            return {"turns": [{"user": "pregunta", "assistant": "**respuesta**"}]}

    class MarkdownDocument(Document):
        def get(self):
            return MarkdownSnapshot()

    class MarkdownCollection(Collection):
        def document(self, _document_id):
            return MarkdownDocument()

    class MarkdownFirestore(FakeFirestore):
        def collection(self, _collection):
            return MarkdownCollection()

    assert runner.verify_firestore_turn(
        MarkdownFirestore(), "chatbot_aad_eval_test", "doc", "pregunta", "*respuesta*"
    )


def test_unverified_extra_numeric_claim_fails_reference_consistency(monkeypatch):
    monkeypatch.setattr(runner, "run_reference_query", lambda *_args: [{"value": 0.71}])
    step = {"reference_checks": [{
        "name": "availability", "sql": "SELECT 1", "value_column": "value",
        "factor": 100, "tolerance": 0.1, "unit": "percent",
        "answer_pattern": "disponibilidad[^0-9]*([0-9.,]+)\\s*%",
    }]}
    result = runner.evaluate_step(
        step, "Disponibilidad 71 %; mejora de 90 %.", None, "p", "europe-west1"
    )
    assert result["categories"]["calculation"] == "PASS"
    assert result["categories"]["numeric_consistency"] == "FAIL"
    assert result["status"] == "FAIL"


def test_missing_reference_value_is_blocked_as_data_not_service_failure(monkeypatch):
    monkeypatch.setattr(runner, "run_reference_query", lambda *_args: [{"value": None}])
    result = runner.evaluate_step({"reference_checks": [{
        "name": "empty availability", "sql": "SELECT NULL AS value",
        "value_column": "value", "answer_pattern": "([0-9.]+)",
    }]}, "Sin datos", None, "p", "europe-west1")
    assert result["categories"]["reference_data"] == "BLOCKED"
    assert "reference_service" not in result["categories"]
    assert result["status"] == "BLOCKED"
