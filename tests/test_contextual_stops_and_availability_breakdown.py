from types import SimpleNamespace

import pytest


ANALYTIC_BREAKDOWN_QUESTION = (
    "Para RB6, del 5 al 9 de octubre de 2026, desglosa el cálculo de la "
    "disponibilidad del 43,20 %: tiempo planificado, tiempo de paradas que "
    "afectan al OEE y tiempo operativo. Si no puedes obtener alguno de esos "
    "valores, indícalo sin estimarlo."
)


def test_analytic_breakdown_is_not_misclassified_as_capability_question(aad_app):
    assert aad_app.capability_response(ANALYTIC_BREAKDOWN_QUESTION, []) is None


def test_genuine_capability_questions_still_get_capability_response(aad_app):
    for question in ("¿Qué puedes hacer?", "¿Para qué sirves?", "¿Cómo trabajas?"):
        answer = aad_app.capability_response(question, [])
        assert answer
        assert "OEE" in answer or "vistas autorizadas" in answer


def test_availability_breakdown_is_oee_mode_not_stops_or_article_breakdown(aad_app):
    assert aad_app.query_mode(ANALYTIC_BREAKDOWN_QUESTION, []) == "global"
    assert aad_app._conversation_domain(ANALYTIC_BREAKDOWN_QUESTION) == "oee"


def test_genuine_stop_cause_question_remains_in_stops_domain(aad_app):
    question = "¿Qué paradas OEE y cuáles son sus causas en RB6?"
    assert aad_app.query_mode(question, []) == "paradas"
    assert aad_app._conversation_domain(question) == "paradas"


def test_contextual_stops_inherit_operational_week_and_current_machine(aad_app):
    history = [{
        "user": "¿Qué máquina tiene peor disponibilidad esta semana y por qué?",
        "assistant": (
            "La máquina con peor disponibilidad entre el 5 y el 9 de octubre de 2026 "
            "es la RB6, con un 43,20 %."
        ),
    }]
    question = (
        "¿Por qué tiene RB6 una disponibilidad tan baja? "
        "¿Qué paradas o pérdidas de tiempo la explican?"
    )
    expected_period = aad_app.resolve_temporal_context("esta semana")
    filters = aad_app.active_filters(question, history)
    sql = aad_app.deterministic_stop_details_sql(question, history)

    assert filters["modo_consulta"] == "paradas"
    assert filters["maquina"] == "RB6"
    assert filters["fecha_desde"] == expected_period["fecha_desde"]
    assert filters["fecha_hasta"] == expected_period["fecha_hasta"]
    assert "fecha_operativa BETWEEN DATE" in sql
    assert f"DATE '{expected_period['fecha_desde']}'" in sql
    assert f"DATE '{expected_period['fecha_hasta']}'" in sql
    assert "UPPER(TRIM(maquina)) = 'RB6'" in sql


def test_semantic_rewrite_cannot_drop_contextual_stop_period(aad_app, monkeypatch):
    history = [{
        "user": "¿Qué máquina tiene peor disponibilidad esta semana y por qué?",
        "assistant": "La máquina con peor disponibilidad esta semana fue RB6, con 43,20 %.",
    }]
    monkeypatch.setattr(
        aad_app,
        "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="Enumera las paradas OEE de RB6."
        ))),
    )
    original = "¿Por qué tiene RB6 una disponibilidad tan baja? ¿Qué paradas la explican?"
    rewritten = aad_app.semantic_rewrite_question(original, "schema test", history)
    filters = aad_app.active_filters(rewritten, history)
    sql = aad_app.deterministic_stop_details_sql(rewritten, history)
    expected = aad_app.resolve_temporal_context("esta semana")

    assert "RB6" in rewritten
    assert filters["maquina"] == "RB6"
    assert filters["fecha_desde"] == expected["fecha_desde"]
    assert filters["fecha_hasta"] == expected["fecha_hasta"]
    assert f"DATE '{expected['fecha_desde']}'" in sql
    assert f"DATE '{expected['fecha_hasta']}'" in sql


def test_explicit_current_period_overrides_inherited_oee_week_even_if_rewrite_drops_it(
    aad_app, monkeypatch
):
    history = [{
        "user": "¿Qué máquina tiene peor disponibilidad esta semana y por qué?",
        "assistant": "La peor fue RB6 durante la semana del 5 al 9 de octubre de 2026.",
    }]
    monkeypatch.setattr(
        aad_app,
        "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="Enumera las paradas OEE de RB6."
        ))),
    )
    original = (
        "¿Por qué tiene RB6 una disponibilidad tan baja? "
        "¿Qué paradas la explican del 8 al 9 de octubre de 2026?"
    )
    rewritten = aad_app.semantic_rewrite_question(original, "schema test", history)
    filters = aad_app.active_filters(rewritten, history)
    sql = aad_app.deterministic_stop_details_sql(rewritten, history)

    assert filters["fecha_desde"] == "2026-10-08"
    assert filters["fecha_hasta"] == "2026-10-09"
    assert "DATE '2026-10-08'" in sql and "DATE '2026-10-09'" in sql
    assert "DATE '2026-10-05'" not in sql


def test_explicit_historical_stop_question_does_not_inherit_oee_week(aad_app):
    history = [{
        "user": "¿Qué máquina tiene peor disponibilidad esta semana y por qué?",
        "assistant": "RB6 tuvo la peor disponibilidad esta semana.",
    }]
    filters = aad_app.active_filters(
        "Históricamente, ¿qué paradas explican la baja disponibilidad de RB6?", history
    )
    assert filters["alcance_temporal"] == "historico"
    assert "fecha_desde" not in filters
    assert "fecha_hasta" not in filters
    assert "semana" not in filters


def test_planner_context_is_not_inherited_by_stop_or_oee_domain(aad_app):
    history = [{
        "user": "Compara la carga pendiente de RB6 en semanas 2026-40 y 2026-41.",
        "assistant": "Carga comparada para RB6 en esas semanas.",
    }]
    filters = aad_app.active_filters("¿Qué paradas hubo en RB6?", history)
    sql = aad_app.deterministic_stop_details_sql("¿Qué paradas hubo en RB6?", history)
    assert filters["modo_consulta"] == "paradas"
    assert filters["maquina"] == "RB6"
    assert "semana" not in filters
    assert "fecha_desde" not in filters
    assert "semana_necesidad" not in sql
    assert "2026-40" not in sql and "2026-41" not in sql


def test_no_stop_rows_are_reported_without_cross_period_causal_claim(aad_app):
    answer = aad_app.explain(
        "¿Qué paradas hubo en RB6 esta semana?",
        [],
        [],
        "SELECT id_parada FROM `aad-analitica-oee.oee_planta.vw_import_paradas`",
    )
    assert "No se encontraron registros de paradas" in answer
    assert "no hay incidencias" in answer
    assert "causa" in answer


def test_availability_breakdown_sql_and_answer_do_not_invent_stop_or_operating_time(aad_app):
    sql = aad_app.deterministic_availability_breakdown_sql(ANALYTIC_BREAKDOWN_QUESTION, [])
    assert "vw_oee_master" in sql
    assert "UPPER(TRIM(maquina)) = 'RB6'" in sql
    assert "fecha BETWEEN DATE '2026-10-05' AND DATE '2026-10-09'" in sql
    assert "SUM(tiempo_plan) AS tiempo_plan" in sql
    assert "SUM(tiempo_erp_capado) AS tiempo_erp_capado" in sql
    assert "WHEN SUM(tiempo_plan) = 0 THEN NULL" in sql
    assert "ELSE SUM(tiempo_erp_capado) / SUM(tiempo_plan)" in sql
    assert "IFNULL" not in sql
    assert "vw_import_paradas" not in sql
    assert "tiempo_plan - tiempo_erp_capado" not in sql

    answer = aad_app.deterministic_availability_breakdown_answer(
        ANALYTIC_BREAKDOWN_QUESTION,
        [{
            "filas_fuente": 2,
            "tiempo_plan": 100.0,
            "tiempo_erp_capado": 43.2,
            "disponibilidad_pct": 0.432,
        }],
        sql,
        [],
    )
    assert "SUM(tiempo_plan) = 100,00" in answer
    assert "SUM(tiempo_erp_capado) = 43,20" in answer
    assert "43.20%" in answer
    assert "sin unidad especificada en el esquema" in answer
    assert "horas" not in answer.lower()
    assert "minutos" not in answer.lower()
    assert "no permite identificar por separado un tiempo operativo" in answer
    assert "no sería verificable" in answer
    assert "tiempo de paradas OEE" not in answer


def test_availability_breakdown_without_rows_is_explicit_and_not_estimated(aad_app):
    sql = aad_app.deterministic_availability_breakdown_sql(ANALYTIC_BREAKDOWN_QUESTION, [])
    answer = aad_app.deterministic_availability_breakdown_answer(
        ANALYTIC_BREAKDOWN_QUESTION, [], sql, []
    )
    assert "No puedo distinguir con seguridad" in answer
    assert "no estimaré tiempos" in answer


@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        (
            {"filas_fuente": 1, "tiempo_plan": 100, "tiempo_erp_capado": None,
             "disponibilidad_pct": None},
            "SUM(tiempo_erp_capado) es NULL",
        ),
        (
            {"filas_fuente": 1, "tiempo_plan": None, "tiempo_erp_capado": 25,
             "disponibilidad_pct": None},
            "SUM(tiempo_plan) es NULL",
        ),
        (
            {"filas_fuente": 1, "tiempo_plan": 0, "tiempo_erp_capado": 0,
             "disponibilidad_pct": None},
            "SUM(tiempo_plan) es cero",
        ),
        (
            {"filas_fuente": 0, "tiempo_plan": None, "tiempo_erp_capado": None,
             "disponibilidad_pct": None},
            "No hay filas de origen",
        ),
    ],
)
def test_breakdown_respects_null_zero_and_empty_source_semantics(
    aad_app, result, expected_text
):
    sql = aad_app.deterministic_availability_breakdown_sql(ANALYTIC_BREAKDOWN_QUESTION, [])
    answer = aad_app.deterministic_availability_breakdown_answer(
        ANALYTIC_BREAKDOWN_QUESTION, [result], sql, []
    )
    assert expected_text in answer
    if result["filas_fuente"]:
        assert "no se sustituye por cero" in answer or result["tiempo_plan"] in (0, None)


@pytest.mark.parametrize(
    "rewritten",
    [
        "Desglosa disponibilidad de RB6 entre el 5 y el 9 de octubre de 2026.",
        "Calcula los componentes de disponibilidad para RB6 entre 2026-10-05 y 2026-10-09.",
        "Descompón el cálculo de disponibilidad de RB6 del 5 al 9 de octubre de 2026.",
    ],
)
def test_original_breakdown_routes_to_its_sql_despite_rewrite_variation(
    aad_app, monkeypatch, rewritten
):
    captured = {}
    monkeypatch.setattr(aad_app, "load_history", lambda _conversation: [])
    monkeypatch.setattr(aad_app, "save_turn", lambda *_args: None)
    monkeypatch.setattr(aad_app, "dataset_schema", lambda: ("synthetic schema", {"vw_oee_master"}))
    monkeypatch.setattr(aad_app, "semantic_rewrite_question", lambda *_args, **_kwargs: rewritten)
    monkeypatch.setattr(aad_app, "business_sql_error", lambda *_args: None)

    def fake_execute(sql):
        captured["sql"] = sql
        return [{
            "filas_fuente": 1,
            "tiempo_plan": 100,
            "tiempo_erp_capado": 43.2,
            "disponibilidad_pct": 0.432,
        }]

    monkeypatch.setattr(aad_app, "execute_query", fake_execute)
    response = aad_app.app.test_client().post("/", json={
        "type": "MESSAGE",
        "message": {
            "text": ANALYTIC_BREAKDOWN_QUESTION,
            "space": {"name": "spaces/test"},
            "sender": {"name": "users/test"},
        },
    })

    assert response.status_code == 200
    assert "vw_oee_master" in captured["sql"]
    assert "UPPER(TRIM(maquina)) = 'RB6'" in captured["sql"]
    assert "DATE '2026-10-05'" in captured["sql"]
    assert "DATE '2026-10-09'" in captured["sql"]
    assert "SUM(tiempo_erp_capado) / SUM(tiempo_plan)" in captured["sql"]
    assert "No estimaré los componentes ausentes" in response.get_json()["text"]


def test_answer_is_bound_to_the_sql_that_was_executed(aad_app):
    history = []
    expected_sql = aad_app.deterministic_availability_breakdown_sql(
        ANALYTIC_BREAKDOWN_QUESTION, history
    )
    row_from_another_query = [{
        "filas_fuente": 1,
        "tiempo_plan": 10,
        "tiempo_erp_capado": 5,
        "disponibilidad_pct": 0.5,
    }]
    unrelated_sql = (
        "SELECT disponibilidad_pct FROM "
        "`aad-analitica-oee.oee_planta.vw_oee_master`"
    )
    assert aad_app.deterministic_availability_breakdown_answer(
        ANALYTIC_BREAKDOWN_QUESTION, row_from_another_query, unrelated_sql, history
    ) is None
    assert aad_app.deterministic_availability_breakdown_answer(
        ANALYTIC_BREAKDOWN_QUESTION, row_from_another_query, expected_sql, history
    )


def test_availability_question_without_breakdown_never_uses_breakdown_answer(aad_app):
    question = "¿Cuál fue la disponibilidad de RB6 en la semana 2026-40?"
    assert aad_app.deterministic_availability_breakdown_sql(question, []) is None
    assert aad_app.deterministic_availability_breakdown_answer(
        question,
        [{"filas_fuente": 1, "tiempo_plan": 100, "tiempo_erp_capado": 50,
          "disponibilidad_pct": 0.5}],
        "SELECT disponibilidad_pct",
        [],
    ) is None


def test_regular_oee_and_planner_sql_keep_their_existing_routes(aad_app):
    oee_sql = aad_app.make_sql(
        "Disponibilidad de RB6 en la semana 2026-40", "schema test", []
    )
    planner_sql = aad_app.make_sql(
        "Carga pendiente de RB8 en semana 2026-40", "schema test", []
    )
    assert "vw_oee_master" in oee_sql
    assert "AS disponibilidad_pct" in oee_sql
    assert "filas_fuente" not in oee_sql
    assert "vw_planificador_capacidad" in planner_sql
    assert "cantidad_pendiente" in planner_sql
