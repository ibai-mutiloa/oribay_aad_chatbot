import pytest


DIRECT = "¿Por qué tiene RB6 una disponibilidad tan baja del 5 al 9 de octubre de 2026?"


def test_causal_availability_direct_question_and_separate_queries(aad_app):
    assert aad_app.is_causal_availability_question(DIRECT)
    queries = aad_app.deterministic_causal_availability_sql(DIRECT, [])
    assert queries and len(queries) == 2
    oee, stops = queries
    assert "vw_oee_master" in oee and "vw_import_paradas" not in oee
    assert "SUM(tiempo_erp_capado) / SUM(tiempo_plan)" in oee
    assert "SUM(tiempo_plan) = 0 THEN NULL" in oee
    assert "vw_import_paradas" in stops and "vw_oee_master" not in stops
    assert "fecha_operativa BETWEEN DATE '2026-10-05' AND DATE '2026-10-09'" in stops
    assert "GROUP BY id_parada" in stops
    assert "IFNULL(tiempo" not in oee + stops
    assert not aad_app.is_causal_availability_question("¿Por qué tiene RB6 un OEE bajo?")


def test_explain_why_with_history_is_still_explanation_and_causal(aad_app):
    history = [{"user": "OEE de RB6", "assistant": "La disponibilidad fue baja."}]
    question = "Explícame por qué RB6 tiene una disponibilidad tan baja en ese periodo"
    assert aad_app.is_explanation_request(question)
    assert aad_app.is_causal_availability_question(question)
    assert aad_app.deterministic_causal_availability_sql(question, history) is None


def test_inherits_machine_and_period_but_explicit_period_replaces_it(aad_app):
    history = [{"user": "Disponibilidad de RB6 del 5 al 9 de octubre de 2026", "assistant": ""}]
    inherited = aad_app.deterministic_causal_availability_sql(
        "Explícame por qué tiene una disponibilidad tan baja en ese periodo", history
    )
    assert inherited and "DATE '2026-10-05'" in inherited[0]
    replaced = aad_app.deterministic_causal_availability_sql(
        "Explícame por qué tiene RB6 una disponibilidad baja del 8 al 9 de octubre de 2026", history
    )
    assert replaced and "DATE '2026-10-08'" in replaced[0]
    assert "DATE '2026-10-05'" not in replaced[0]
    assert "DATE '2026-10-08'" in replaced[1]


def test_requires_machine_and_period(aad_app):
    assert aad_app.deterministic_causal_availability_sql(
        "¿Por qué tiene una disponibilidad tan baja del 5 al 9 de octubre de 2026?", []
    ) is None
    assert aad_app.deterministic_causal_availability_sql(
        "¿Por qué tiene RB6 una disponibilidad tan baja?", []
    ) is None


def test_oee_null_and_zero_denominator_are_reported_as_null(aad_app):
    answer = aad_app.deterministic_causal_availability_answer(
        [{"filas_fuente": 2, "tiempo_plan": 0, "tiempo_erp_capado": None, "disponibilidad_pct": None}],
        [{"total_paradas": 0}],
    )
    assert "SUM(tiempo_erp_capado): NULL" in answer
    assert "disponibilidad" in answer and "NULL" in answer


def test_no_stops_does_not_claim_no_losses(aad_app):
    answer = aad_app.deterministic_causal_availability_answer(
        [{"filas_fuente": 1, "tiempo_plan": 10, "tiempo_erp_capado": 5, "disponibilidad_pct": .5}],
        [{"total_paradas": 0}],
    )
    assert "No hay paradas registradas" in answer
    assert "no demuestra que no hubiera pérdidas" in answer


def test_stop_query_failure_is_not_zero_records(aad_app):
    answer = aad_app.deterministic_causal_availability_answer([], None, stop_error="RuntimeError")
    assert "consulta de paradas falló" in answer
    assert "no se puede determinar si hubo registros" in answer
    assert "0 paradas" not in answer


def test_stop_rows_are_deduplicated_and_oee_labels_are_not_causality(aad_app):
    sql = aad_app.deterministic_causal_availability_sql(DIRECT, [])[1]
    assert "GROUP BY id_parada" in sql
    assert "COUNTIF(UPPER(TRIM(oee)) = 'SI') AS etiquetas_si" in sql
    assert "COUNTIF(UPPER(TRIM(oee)) = 'NO') AS etiquetas_no" in sql
    assert "etiquetas_desconocidas" in sql
    assert "'DESCONOCIDA'" in sql and "'INCONSISTENTE'" in sql
    assert "LOGICAL_OR" not in sql
    assert "COUNTIF(tiempo_parada_min IS NULL)" in sql
    assert "IFNULL(tiempo_parada_min" not in sql
    answer = aad_app.deterministic_causal_availability_answer(
        [{"filas_fuente": 1, "tiempo_plan": 10, "tiempo_erp_capado": 5, "disponibilidad_pct": .5}],
        [{"total_paradas": 4, "paradas_oee_si": 1, "tiempo_oee_si": 5,
          "paradas_si_minutos_desconocidos": 0,
          "paradas_oee_no": 1, "tiempo_oee_no": 3,
          "paradas_no_minutos_desconocidos": 0,
          "paradas_oee_desconocidas": 1, "tiempo_oee_desconocido": None,
          "paradas_oee_inconsistentes": 1, "tiempo_oee_inconsistente": 7}],
        machine="RB6",
        period="2026-10-05–2026-10-09",
    )
    assert "Incidencias deduplicadas: 4" in answer
    assert "oee='SI': 1 incidencias" in answer
    assert "Etiqueta desconocida/NULL: 1 incidencias" in answer
    assert "Etiquetas inconsistentes: 1 incidencias" in answer
    assert "Alcance aplicado: máquina RB6; periodo 2026-10-05–2026-10-09" in answer
    assert "unidad de tiempo de estos campos no está documentada" in answer
    assert "no cuantifican por sí solas" in answer
    assert "Datos comprobados" in answer and "Paradas registradas" in answer and "Qué puede concluirse" in answer


def test_normal_explanation_of_previous_answer_remains_classified(aad_app):
    assert aad_app.is_explanation_request("Explícame por qué dijiste que RB6 fue la peor")
    assert not aad_app.is_causal_availability_question("Explícame por qué dijiste que RB6 fue la peor")


def test_current_explicit_period_wins_over_older_historical_context(aad_app):
    history = [
        {"user": "Disponibilidad de RB6 en la semana 2024-10", "assistant": "Dato histórico."},
        {"user": "Disponibilidad de RB6 en la semana 2025-20", "assistant": "Otro periodo."},
    ]
    filters = aad_app.causal_availability_filters(
        "¿Por qué tiene RB6 disponibilidad baja del 5 al 9 de octubre de 2026?", history
    )
    sql = aad_app.deterministic_causal_availability_sql(
        "¿Por qué tiene RB6 disponibilidad baja del 5 al 9 de octubre de 2026?", history
    )
    assert filters["fecha_desde"] == "2026-10-05"
    assert filters["fecha_hasta"] == "2026-10-09"
    assert sql and "DATE '2026-10-05'" in sql[0]
    assert "2024-10" not in sql[0] and "2025-20" not in sql[0]


def test_old_historical_scope_is_not_inherited_through_period_reference(aad_app):
    history = [{
        "user": "Históricamente, revisa la disponibilidad baja de RB6.",
        "assistant": "Se consideró todo el histórico disponible.",
    }]
    question = "¿Por qué tiene RB6 una disponibilidad baja en ese mismo periodo?"
    filters = aad_app.causal_availability_filters(question, history)
    assert filters.get("requiere_aclaracion") == "periodo"
    assert "alcance_temporal" not in filters
    assert aad_app.deterministic_causal_availability_sql(question, history) is None


def test_older_period_is_not_inherited_after_a_new_unscoped_oee_turn(aad_app):
    history = [
        {"user": "Disponibilidad de RB6 en la semana 2025-20.", "assistant": "Resultado para 2025-20."},
        {"user": "¿Por qué tuvo RB6 baja disponibilidad?", "assistant": "La disponibilidad fue baja."},
    ]
    question = "¿Por qué tuvo RB6 una disponibilidad baja en ese mismo periodo?"
    filters = aad_app.causal_availability_filters(question, history)
    assert filters.get("requiere_aclaracion") == "periodo"
    assert "semana" not in filters and "fecha_desde" not in filters and "anio" not in filters
    assert aad_app.deterministic_causal_availability_sql(question, history) is None


def test_multiple_machines_require_user_selection(aad_app):
    question = "¿Por qué tienen RB6 o RB7 una disponibilidad baja del 5 al 9 de octubre de 2026?"
    filters = aad_app.causal_availability_filters(question, [])
    assert filters.get("maquinas") == "RB6,RB7"
    assert aad_app.deterministic_causal_availability_sql(question, []) is None


def test_valid_period_reference_inherits_only_the_latest_explicit_oee_scope(aad_app):
    history = [{
        "user": "Disponibilidad de RB6 entre el 5 y el 9 de octubre de 2026.",
        "assistant": "RB6 tuvo disponibilidad baja entre esas fechas.",
    }]
    question = "¿Por qué tiene RB6 una disponibilidad baja en ese mismo periodo?"
    filters = aad_app.causal_availability_filters(question, history)
    sql = aad_app.deterministic_causal_availability_sql(question, history)
    assert filters["fecha_desde"] == "2026-10-05"
    assert filters["fecha_hasta"] == "2026-10-09"
    assert sql and "fecha_operativa BETWEEN DATE '2026-10-05' AND DATE '2026-10-09'" in sql[1]


def test_null_and_inconsistent_stop_labels_and_null_minutes_are_not_reclassified(aad_app):
    sql = aad_app.deterministic_causal_availability_sql(DIRECT, [])[1]
    assert "COUNTIF(oee IS NULL OR UPPER(TRIM(oee)) NOT IN ('SI', 'NO'))" in sql
    assert "WHEN etiquetas_si > 0 AND etiquetas_no > 0 THEN 'INCONSISTENTE'" in sql
    assert "ELSE 'DESCONOCIDA'" in sql
    assert "tramos_minutos_desconocidos = 0" in sql
    answer = aad_app.deterministic_causal_availability_answer(
        [{"filas_fuente": 1, "tiempo_plan": 10, "tiempo_erp_capado": 5,
          "disponibilidad_pct": .5}],
        [{"total_paradas": 1, "paradas_oee_si": 1, "tiempo_oee_si": None,
          "paradas_si_minutos_desconocidos": 1, "paradas_oee_no": 0,
          "tiempo_oee_no": None, "paradas_no_minutos_desconocidos": 0,
          "paradas_oee_desconocidas": 0, "tiempo_oee_desconocido": None,
          "paradas_oee_inconsistentes": 0, "tiempo_oee_inconsistente": None}],
        machine="RB6", period="2026-10-05–2026-10-09",
    )
    assert "suma de minutos de incidencias con todos sus tramos conocidos: NULL" in answer
    assert "duración desconocida por minutos NULL en 1 incidencias" in answer


def test_chat_event_executes_two_separate_causal_queries_and_reports_applied_scope(aad_app, monkeypatch):
    calls = []
    validations = []
    monkeypatch.setattr(aad_app, "load_history", lambda _conversation: [])
    monkeypatch.setattr(aad_app, "save_turn", lambda *_args: None)
    monkeypatch.setattr(aad_app, "dataset_schema", lambda: ("schema", {"vw_oee_master", "vw_import_paradas"}))
    monkeypatch.setattr(aad_app, "validate_sql", lambda sql, views: validations.append((sql, views)))

    def execute(sql):
        calls.append(sql)
        if "vw_oee_master" in sql:
            return [{"filas_fuente": 1, "tiempo_plan": 100, "tiempo_erp_capado": 40,
                     "disponibilidad_pct": .4}]
        return [{"total_paradas": 0}]

    monkeypatch.setattr(aad_app, "execute_query", execute)
    response = aad_app.app.test_client().post("/", json={
        "type": "MESSAGE",
        "message": {"text": DIRECT, "space": {"name": "spaces/test"},
                    "sender": {"name": "users/test"}},
    })
    text = response.get_json()["text"]
    assert response.status_code == 200
    assert len(calls) == 2 and len(validations) == 2
    assert "vw_oee_master" in calls[0] and "vw_import_paradas" not in calls[0]
    assert "vw_import_paradas" in calls[1] and "vw_oee_master" not in calls[1]
    assert "Alcance aplicado: máquina RB6; periodo 2026-10-05–2026-10-09" in text


def test_chat_event_explanation_history_still_bypasses_queries(aad_app, monkeypatch):
    calls = []
    history = [{"user": "Ranking de disponibilidad de RB6", "assistant": "RB6 fue la peor."}]
    monkeypatch.setattr(aad_app, "load_history", lambda _conversation: history)
    monkeypatch.setattr(aad_app, "save_turn", lambda *_args: None)
    monkeypatch.setattr(aad_app, "explain_history", lambda *_args: "Explicación histórica.")
    monkeypatch.setattr(aad_app, "execute_query", lambda sql: calls.append(sql))
    monkeypatch.setattr(aad_app, "dataset_schema", lambda: pytest.fail("no debe consultar esquema"))
    response = aad_app.app.test_client().post("/", json={
        "type": "MESSAGE",
        "message": {"text": "Explícame por qué dijiste que RB6 fue la peor.",
                    "space": {"name": "spaces/test"},
                    "sender": {"name": "users/test"}},
    })
    assert response.get_json()["text"] == "Explicación histórica."
    assert not calls


def test_chat_event_partial_stop_query_error_is_not_reported_as_no_records(aad_app, monkeypatch):
    calls = []
    monkeypatch.setattr(aad_app, "load_history", lambda _conversation: [])
    monkeypatch.setattr(aad_app, "save_turn", lambda *_args: None)
    monkeypatch.setattr(aad_app, "dataset_schema", lambda: ("schema", {"vw_oee_master", "vw_import_paradas"}))
    monkeypatch.setattr(aad_app, "validate_sql", lambda *_args: None)

    def execute(sql):
        calls.append(sql)
        if "vw_import_paradas" in sql:
            raise RuntimeError("synthetic outage")
        return [{"filas_fuente": 1, "tiempo_plan": 100, "tiempo_erp_capado": 40,
                 "disponibilidad_pct": .4}]

    monkeypatch.setattr(aad_app, "execute_query", execute)
    response = aad_app.app.test_client().post("/", json={
        "type": "MESSAGE",
        "message": {"text": DIRECT, "space": {"name": "spaces/test"},
                    "sender": {"name": "users/test"}},
    })
    text = response.get_json()["text"]
    assert len(calls) == 2
    assert "consulta de paradas falló" in text
    assert "No hay paradas registradas" not in text


def test_chat_event_ambiguous_machines_asks_before_any_query(aad_app, monkeypatch):
    calls = []
    monkeypatch.setattr(aad_app, "load_history", lambda _conversation: [])
    monkeypatch.setattr(aad_app, "save_turn", lambda *_args: None)
    monkeypatch.setattr(aad_app, "dataset_schema", lambda: pytest.fail("no debe consultar esquema"))
    monkeypatch.setattr(aad_app, "execute_query", lambda sql: calls.append(sql))
    question = "¿Por qué tienen RB6 o RB7 disponibilidad baja del 5 al 9 de octubre de 2026?"
    response = aad_app.app.test_client().post("/", json={
        "type": "MESSAGE",
        "message": {"text": question, "space": {"name": "spaces/test"},
                    "sender": {"name": "users/test"}},
    })
    text = response.get_json()["text"]
    assert "RB6 o RB7" in text
    assert not calls
