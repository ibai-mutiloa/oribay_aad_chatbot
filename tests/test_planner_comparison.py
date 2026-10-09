from copy import deepcopy


QUESTION = "Compara la carga pendiente de RB8 entre las semanas 2026-42 y 2026-43"


def comparison_sql(app, question=QUESTION):
    sql = app.deterministic_planner_week_load_sql(question, [])
    assert sql is not None
    return sql


def comparison_rows(first, second):
    return [
        {
            "anio_iso": 2026,
            "semana_necesidad": 42,
            "maquina": "RB8",
            "cantidad_pendiente": first,
            "ordenes": 3,
        },
        {
            "anio_iso": 2026,
            "semana_necesidad": 43,
            "maquina": "RB8",
            "cantidad_pendiente": second,
            "ordenes": 7,
        },
    ]


def test_planner_comparison_summarizes_real_values_and_iso_periods(aad_app):
    sql = comparison_sql(aad_app)
    rows = comparison_rows(85568, 26464)

    assert "EXTRACT(ISOYEAR FROM fecha_necesidad) = 2026" in sql
    assert "CAST(semana_necesidad AS STRING) = '42'" in sql
    assert "CAST(semana_necesidad AS STRING) = '43'" in sql
    assert "GROUP BY EXTRACT(ISOYEAR FROM fecha_necesidad), semana_necesidad, maquina" in sql
    answer = aad_app.deterministic_planner_comparison_answer(QUESTION, rows, sql)
    assert "85.568 unidades" in answer
    assert "26.464 unidades" in answer
    assert "59.104 unidades" in answer
    assert "una disminución del 69,07%" in answer


def test_planner_comparison_handles_increase_and_no_change(aad_app):
    sql = comparison_sql(aad_app)
    increased = aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(100, 125), sql
    )
    unchanged = aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(100, 100), sql
    )

    assert "aumentó" in increased
    assert "25 unidades" in increased
    assert "un aumento del 25,00%" in increased
    assert "se mantuvo sin variación" in unchanged
    assert "0 unidades" in unchanged
    assert "variación relativa fue del 0,00%" in unchanged


def test_planner_comparison_zero_initial_value_never_divides_or_invents_percent(aad_app):
    answer = aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(0, 12), comparison_sql(aad_app)
    )
    assert "aumentó" in answer
    assert "12 unidades" in answer
    assert "no se puede calcular porque la carga inicial es cero" in answer
    assert "%" not in answer


def test_planner_comparison_reports_first_period_missing_without_computing_delta(aad_app):
    sql = comparison_sql(aad_app)
    rows = [comparison_rows(85568, 26464)[1]]
    answer = aad_app.deterministic_planner_comparison_answer(QUESTION, rows, sql)

    assert "26.464 unidades" in answer
    assert "semana 2026-43" in answer
    assert "en la semana 2026-42 no se encontraron registros de carga pendiente positiva" in answer
    assert "no se puede calcular la diferencia" in answer
    assert "59.104" not in answer
    assert "%" not in answer


def test_planner_comparison_reports_second_period_missing_without_computing_delta(aad_app):
    rows = [comparison_rows(85568, 26464)[0]]
    answer = aad_app.deterministic_planner_comparison_answer(
        QUESTION, rows, comparison_sql(aad_app)
    )
    assert "85.568 unidades" in answer
    assert "semana 2026-42" in answer
    assert "en la semana 2026-43 no se encontraron registros de carga pendiente positiva" in answer
    assert "diferencia absoluta" not in answer
    assert "%" not in answer


def test_planner_comparison_with_no_rows_keeps_fallback_path(aad_app):
    assert aad_app.deterministic_planner_comparison_answer(
        QUESTION, [], comparison_sql(aad_app)
    ) is None


def test_planner_comparison_preserves_reverse_requested_period_order(aad_app):
    question = "Compara la carga pendiente de RB8 entre las semanas 2026-43 y 2026-42"
    rows = comparison_rows(85568, 26464)
    answer = aad_app.deterministic_planner_comparison_answer(
        question, rows, comparison_sql(aad_app, question)
    )
    assert answer.index("26.464 unidades en la semana 2026-43") < answer.index(
        "85.568 unidades en la semana 2026-42"
    )
    assert "aumentó" in answer

    only_requested_first = [comparison_rows(85568, 26464)[1]]
    partial = aad_app.deterministic_planner_comparison_answer(
        question, only_requested_first, comparison_sql(aad_app, question)
    )
    assert partial.index("semana 2026-43 se encontraron") < partial.index(
        "semana 2026-42 no se encontraron"
    )


def test_planner_comparison_declines_incomplete_or_ambiguous_rows(aad_app):
    sql = comparison_sql(aad_app)
    missing_value = comparison_rows(100, 90)
    del missing_value[1]["cantidad_pendiente"]
    duplicate_period = comparison_rows(100, 90)
    duplicate_period[1]["semana_necesidad"] = 42
    different_machines = comparison_rows(100, 90)
    different_machines[1]["maquina"] = "RB7"
    wrong_week = comparison_rows(100, 90)
    wrong_week[1]["semana_necesidad"] = 44
    invalid_sql = sql.replace("CAST(semana_necesidad AS STRING) = '43'", "CAST(semana_necesidad AS STRING) = '44'")

    for rows in (missing_value, duplicate_period, different_machines, wrong_week):
        assert aad_app.deterministic_planner_comparison_answer(QUESTION, rows, sql) is None

    assert aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(100, 90), invalid_sql
    ) is None
    two_machine_sql = sql.replace(
        "UPPER(TRIM(maquina)) = 'RB8'", "UPPER(TRIM(maquina)) IN ('RB8', 'RB7')"
    )
    assert aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(100, 90), two_machine_sql
    ) is None

    assert aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(100, 90) + [deepcopy(comparison_rows(100, 90)[0])], sql
    ) is None


def test_planner_comparison_trace_names_both_sql_periods(aad_app):
    sql = comparison_sql(aad_app)
    answer = aad_app.deterministic_planner_comparison_answer(
        QUESTION, comparison_rows(85568, 26464), sql
    )
    traced = aad_app.append_calculation_trace(answer, QUESTION, comparison_rows(85568, 26464), sql, [])

    assert "semanas de necesidad ISO 2026-42 y 2026-43" in traced
    assert "máquina RB8" in traced
    assert "la semana 2026-42, máquina RB8" not in traced

    partial = aad_app.deterministic_planner_comparison_answer(
        QUESTION, [comparison_rows(85568, 26464)[0]], sql
    )
    partial_trace = aad_app.append_calculation_trace(
        partial, QUESTION, [comparison_rows(85568, 26464)[0]], sql, []
    )
    assert "semanas de necesidad ISO 2026-42 y 2026-43" in partial_trace
    assert "máquina RB8" in partial_trace


def test_planner_single_week_sql_and_trace_keep_the_requested_period(aad_app):
    question = "Carga pendiente de RB8 en la semana 2026-42"
    sql = aad_app.deterministic_planner_week_load_sql(question, [])
    rows = [{
        "semana_necesidad": 42,
        "maquina": "RB8",
        "cantidad_pendiente": 85568,
        "ordenes": 3,
    }]
    answer = aad_app.deterministic_planner_comparison_answer(question, rows, sql)
    assert answer is None
    assert "CAST(semana_necesidad AS STRING) IN ('42')" in sql
    traced = aad_app.append_calculation_trace("Resultado", question, rows, sql, [])
    assert "semana de necesidad 42" in traced
    assert "año calendario 2026" in traced
    assert "semanas de necesidad ISO 2026-42 y 2026-43" not in traced


def test_comparison_sql_keeps_year_week_pairs_distinct_across_iso_years(aad_app):
    question = "Compara la carga pendiente de RB8 entre las semanas 2026-52 y 2027-01"
    sql = comparison_sql(aad_app, question)
    assert "EXTRACT(ISOYEAR FROM fecha_necesidad) = 2026" in sql
    assert "CAST(semana_necesidad AS STRING) = '52'" in sql
    assert "EXTRACT(ISOYEAR FROM fecha_necesidad) = 2027" in sql
    assert "CAST(semana_necesidad AS STRING) = '1'" in sql
    assert "EXTRACT(YEAR FROM fecha_necesidad)" not in sql


def test_comparison_declines_same_week_number_when_row_iso_year_is_unverifiable(aad_app):
    question = "Compara la carga pendiente de RB8 entre las semanas 2025-01 y 2026-01"
    sql = comparison_sql(aad_app, question)
    row_without_year = {
        "semana_necesidad": 1,
        "maquina": "RB8",
        "cantidad_pendiente": 50,
    }
    assert aad_app.deterministic_planner_comparison_answer(
        question, [row_without_year], sql
    ) is None


def test_planner_comparison_supports_one_present_row_across_iso_years(aad_app):
    question = "Compara la carga pendiente de RB8 entre las semanas 2026-52 y 2027-01"
    sql = comparison_sql(aad_app, question)
    rows = [{
        "anio_iso": 2027,
        "semana_necesidad": 1,
        "maquina": "RB8",
        "cantidad_pendiente": 50,
    }]
    answer = aad_app.deterministic_planner_comparison_answer(question, rows, sql)
    assert "en la semana 2027-01 se encontraron 50 unidades de carga pendiente positiva" in answer
    assert "en la semana 2026-52 no se encontraron registros de carga pendiente positiva" in answer
    assert "%" not in answer


def test_elliptical_planner_follow_up_inherits_pending_load_intent_and_machine(aad_app):
    history = [{
        "user": QUESTION,
        "assistant": "La carga pendiente de RB8 disminuyó entre ambas semanas.",
    }]
    question = "¿Y entre las semanas 2026-41 y 2026-42?"
    # Model output has dropped both the comparison wording and the machine.
    rewritten = "Carga pendiente en las semanas 2026-41 y 2026-42"
    filters = aad_app.active_filters(rewritten, history)
    sql = aad_app.deterministic_planner_week_load_sql(rewritten, history)
    rows = [{
        "anio_iso": 2026,
        "semana_necesidad": 42,
        "maquina": "RB8",
        "cantidad_pendiente": 100,
    }]

    assert filters.get("maquina") == "RB8", filters
    assert "UPPER(TRIM(maquina)) = 'RB8'" in sql
    assert "EXTRACT(ISOYEAR FROM fecha_necesidad) = 2026" in sql
    assert "CAST(semana_necesidad AS STRING) = '41'" in sql
    assert "CAST(semana_necesidad AS STRING) = '42'" in sql
    answer = aad_app.deterministic_planner_comparison_answer(rewritten, rows, sql, history)
    assert "RB8" in answer
    assert "en la semana 2026-41 no se encontraron registros de carga pendiente positiva" in answer
    assert "en la semana 2026-42 se encontraron 100 unidades de carga pendiente positiva" in answer
    assert "%" not in answer
