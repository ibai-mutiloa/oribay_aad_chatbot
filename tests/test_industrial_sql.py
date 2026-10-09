import pytest


def test_single_oee_sql_uses_corporate_weighted_formulas(aad_app):
    sql = aad_app.deterministic_single_kpi_sql("Dame OEE y calidad de RB4 en semana 2026-22", [])
    assert "SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas" in sql
    assert "SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct" in sql
    assert "SAFE_DIVIDE(total_piezas_producidas, piezas_teo)" in sql
    assert "calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct" in sql
    assert "AVG(" not in sql.upper()
    assert "semana = '2026-22'" in sql


def test_machine_ranking_uses_requested_metric_and_order(aad_app):
    sql = aad_app.deterministic_machine_metric_ranking_sql(
        "peor máquina en rendimiento semana 2026-22", []
    )
    assert "rendimiento_pct" in sql
    assert "ORDER BY rendimiento_pct ASC" in sql
    assert "REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')" in sql
    assert "semana = '2026-22'" in sql


def test_oee_difference_uses_percentage_points(aad_app):
    sql = aad_app.deterministic_machine_metric_difference_sql(
        "¿Cuántos puntos de OEE separan RB4 y RB7 en 2026-22?", []
    )
    assert "ABS(valor_maquina_1_pct - valor_maquina_2_pct) * 100" in sql
    assert "IN ('RB4', 'RB7')" in sql
    assert "semana = '2026-22'" in sql


def test_stop_summary_consolidates_ids_and_maps_si_no(aad_app):
    sql = aad_app.deterministic_stop_summary_sql("resumen de paradas de RB4", [])
    assert "GROUP BY id_parada, maquina, oee" in sql
    assert "COUNTIF(UPPER(TRIM(oee)) = 'SI') AS paradas_no_planificadas" in sql
    assert "COUNTIF(UPPER(TRIM(oee)) = 'NO') AS paradas_planificadas" in sql


def test_long_stop_ranking_filters_oee_and_sums_segments_before_limit(aad_app):
    sql = aad_app.deterministic_top_stops_sql(
        "cinco paradas OEE más largas de RB4 en semana 2026-35", []
    )
    assert "UPPER(TRIM(oee)) = 'SI'" in sql
    assert "SUM(IFNULL(tiempo_parada_min, 0)) AS tiempo_parada_min" in sql
    assert "GROUP BY id_parada" in sql
    assert sql.index("GROUP BY id_parada") < sql.index("LIMIT 5")


def test_stop_details_returns_ids_and_full_time_bounds(aad_app):
    sql = aad_app.deterministic_stop_details_sql("lista las averías de RB4", [])
    assert "id_parada" in sql
    assert "MIN(inicio_real) AS inicio_real" in sql
    assert "MAX(fin_real) AS fin_real" in sql
    assert "REGEXP_CONTAINS(UPPER(tipo_incidencia), r'AVER[IÍ]A')" in sql


def test_planner_load_aggregates_turn_deviation_and_pending_quantity(aad_app):
    sql = aad_app.deterministic_planner_week_load_sql("qué carga pendiente tiene RB5 en semana 39", [])
    assert "SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente" in sql
    assert "SUM(IFNULL(turnos_aplicados, 0))" in sql
    assert "- SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion" in sql
    assert "CAST(semana_necesidad AS STRING) IN ('39')" in sql


def test_balance_suggestions_read_precomputed_view_and_explain_capacity(aad_app):
    sql = aad_app.deterministic_balance_suggestions_sql("sugerencias de balanceo para RB12 semana 37", [])
    assert "vw_sugerencias_balanceo" in sql
    assert "turnos_origen_liberados" in sql
    assert "turnos_destino_nuevos" in sql
    assert "SAFE_CAST(semana AS INT64) = 37" in sql


def test_balance_suggestions_constrain_requested_year(aad_app):
    question = "sugerencias de balanceo semana 2026-42"
    filters = aad_app.active_filters(question, [])
    sql = aad_app.deterministic_balance_suggestions_sql(question, [])
    assert filters["semana"] == "2026-42"
    assert filters["anio"] == "2026"
    assert "EXTRACT(ISOYEAR FROM fecha) = 2026" in sql
    assert "SAFE_CAST(semana AS INT64) = 42" in sql
    assert "EXTRACT(YEAR FROM fecha)" not in sql


def test_balance_suggestions_same_week_number_in_other_year_is_distinct(aad_app):
    question = "sugerencias de balanceo semana 2025-42"
    sql = aad_app.deterministic_balance_suggestions_sql(question, [])
    assert "EXTRACT(ISOYEAR FROM fecha) = 2025" in sql
    assert "SAFE_CAST(semana AS INT64) = 42" in sql
    assert "EXTRACT(ISOYEAR FROM fecha) = 2026" not in sql


def test_balance_suggestions_week_one_uses_iso_year(aad_app):
    question = "sugerencias de balanceo semana 2025-01"
    sql = aad_app.deterministic_balance_suggestions_sql(question, [])
    assert "EXTRACT(ISOYEAR FROM fecha) = 2025" in sql
    assert "SAFE_CAST(semana AS INT64) = 1" in sql
    assert "EXTRACT(YEAR FROM fecha)" not in sql


def test_balance_suggestions_week_without_explicit_year_keeps_week_only_filter(aad_app):
    question = "sugerencias de balanceo semana 42"
    filters = aad_app.active_filters(question, [])
    sql = aad_app.deterministic_balance_suggestions_sql(question, [])
    assert filters["semana"].endswith("-42")
    assert "anio" not in filters
    assert "SAFE_CAST(semana AS INT64) = 42" in sql
    assert "EXTRACT(ISOYEAR FROM fecha)" not in sql


def test_balance_suggestions_without_week_keeps_unbounded_week_scope(aad_app):
    sql = aad_app.deterministic_balance_suggestions_sql("sugerencias de balanceo", [])
    assert "piezas_a_mover > 0" in sql
    assert "turnos_destino_nuevos > 0" in sql
    assert "SAFE_CAST(semana AS INT64)" not in sql
    assert "EXTRACT(ISOYEAR FROM fecha)" not in sql


def test_target_oee_simulation_encodes_proportional_gap_formula(aad_app):
    sql = aad_app.deterministic_oee_target_savings_sql(
        "¿Cuánto ahorraríamos si subimos el OEE al 65 % para RB4 en 2026?", []
    )
    assert "0.650000 AS oee_objetivo_pct" in sql
    assert "(oee_objetivo_pct - oee_actual_pct, 1 - oee_actual_pct)" in sql
    assert "perdida_actual_oee_eur * LEAST" in sql
    assert "cierre_proporcional_de_la_brecha_hasta_oee_100" in sql


def test_synthetic_economic_scenario_matches_expected_values(aad_app, synthetic_oee_rows):
    # Fixture math: (0.65 - 0.50) / (1 - 0.50) = 0.30; €1,000 * 0.30 = €300.
    answer = aad_app.deterministic_oee_target_savings_answer(synthetic_oee_rows)
    assert "ahorro estimado de 300,00 €" in answer
    assert "700,00 €" in answer
    assert "estimación, no un ahorro garantizado" in answer


def test_simulation_has_zero_savings_when_target_does_not_exceed_actual(aad_app):
    row = {
        "oee_actual_pct": 0.70,
        "oee_objetivo_pct": 0.65,
        "perdida_actual_oee_eur": 1000,
        "ahorro_estimado_eur": 0,
        "perdida_restante_estimada_eur": 1000,
    }
    answer = aad_app.deterministic_oee_target_savings_answer([row])
    assert "no genera ahorro adicional" in answer


def test_execute_query_annotates_si_no_and_obeys_byte_limit(aad_app, monkeypatch):
    class FakeJob:
        total_bytes_processed = 10

        def result(self, max_results):
            assert max_results == aad_app.MAX_RESULT_ROWS
            return [{"oee": "SI"}, {"oee": "NO"}]

    class FakeBigQuery:
        def __init__(self):
            self.calls = 0

        def query(self, sql, job_config, location):
            assert sql == "SELECT fixture"
            self.calls += 1
            if self.calls == 1:
                assert job_config.dry_run is True
                return FakeJob()
            assert job_config.maximum_bytes_billed == aad_app.MAXIMUM_BYTES_BILLED
            return FakeJob()

    client = FakeBigQuery()
    monkeypatch.setattr(aad_app, "bq", client)
    rows = aad_app.execute_query("SELECT fixture")
    assert rows == [
        {"oee": "SI", "significado_oee": "OEE / no planificada / incidencia"},
        {"oee": "NO", "significado_oee": "No OEE / planificada"},
    ]
    assert client.calls == 2


def test_execute_query_rejects_dry_run_over_budget(aad_app, monkeypatch):
    class OversizedDryJob:
        total_bytes_processed = aad_app.MAXIMUM_BYTES_BILLED + 1

    class FakeBigQuery:
        def __init__(self):
            self.calls = 0

        def query(self, sql, job_config, location):
            self.calls += 1
            return OversizedDryJob()

    client = FakeBigQuery()
    monkeypatch.setattr(aad_app, "bq", client)
    with pytest.raises(ValueError, match="supera el límite"):
        aad_app.execute_query("SELECT fixture")
    assert client.calls == 1
