from datetime import date


def test_explicit_date_and_invalid_date(aad_app):
    assert aad_app.resolve_temporal_context("el 4 de septiembre de 2026", date(2026, 10, 9)) == {
        "fecha_desde": "2026-09-04", "fecha_hasta": "2026-09-04"
    }
    assert aad_app.resolve_temporal_context("31 de febrero de 2026", date(2026, 10, 9)) == {
        "error_fecha": "La fecha indicada no es válida."
    }


def test_natural_week_is_monday_through_sunday(aad_app):
    actual = aad_app.resolve_temporal_context("semana pasada", date(2026, 10, 9))
    assert actual == {"fecha_desde": "2026-09-28", "fecha_hasta": "2026-10-04"}


def test_closed_quarter_and_calendar_month(aad_app):
    assert aad_app.resolve_temporal_context("último trimestre", date(2026, 10, 9)) == {
        "fecha_desde": "2026-07-01", "fecha_hasta": "2026-09-30"
    }
    assert aad_app.resolve_temporal_context("mes de agosto de 2026", date(2026, 10, 9)) == {
        "fecha_desde": "2026-08-01", "fecha_hasta": "2026-08-31"
    }


def test_relative_weekday_is_previous_occurrence(aad_app):
    assert aad_app.resolve_temporal_context("viernes pasado", date(2026, 10, 9)) == {
        "fecha_desde": "2026-10-02", "fecha_hasta": "2026-10-02"
    }


def test_production_week_is_kept_as_business_week_key(aad_app):
    filters = aad_app.active_filters("OEE RB4 semana 2026-22", [])
    sql = aad_app.deterministic_single_kpi_sql("OEE RB4 semana 2026-22", [])
    assert filters["semana"] == "2026-22"
    assert "semana = '2026-22'" in sql
    assert "fecha BETWEEN" not in sql


def test_calendar_week_is_a_date_range_not_production_week(aad_app):
    filters = aad_app.active_filters("OEE de RB4 la semana pasada", [])
    sql = aad_app.deterministic_single_kpi_sql("OEE de RB4 la semana pasada", [])
    assert filters["fecha_desde"] == "2026-09-28"
    assert filters["fecha_hasta"] == "2026-10-04"
    assert "fecha BETWEEN DATE '2026-09-28' AND DATE '2026-10-04'" in sql
    assert "semana = '2026-" not in sql


def test_planning_weeks_are_parsed_separately(aad_app):
    assert aad_app.planner_requested_weeks("cartera para s38 y s39") == [38, 39]
    assert aad_app.planner_requested_weeks("plan para 2026-38 y 2026-39") == [38, 39]


def test_planner_sql_uses_need_week_and_need_year(aad_app):
    sql = aad_app.deterministic_planner_week_load_sql("carga pendiente RB5 semana 38 de 2026", [])
    assert "vw_planificador_capacidad" in sql
    assert "CAST(semana_necesidad AS STRING) IN ('38')" in sql
    assert "EXTRACT(YEAR FROM fecha_necesidad) = 2026" in sql
    assert "WHERE semana = '2026-38'" not in sql
