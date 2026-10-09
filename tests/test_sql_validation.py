import pytest


def test_accepts_authorized_read_query_and_cte(aad_app):
    sql = """WITH x AS (SELECT maquina FROM `aad-analitica-oee.oee_planta.vw_oee_master`)
SELECT maquina FROM x"""
    assert aad_app.validate_sql(sql, {"vw_oee_master"}) is None


@pytest.mark.parametrize("sql", [
    "DELETE FROM `aad-analitica-oee.oee_planta.vw_oee_master`",
    "SELECT 1; SELECT 2",
    "SELECT * FROM `otro-proyecto.dataset.vista`",
    "SELECT * FROM `aad-analitica-oee.oee_planta.vw_planificador_capacidad`",
])
def test_rejects_writes_multiple_statements_and_unapproved_views(aad_app, sql):
    with pytest.raises(ValueError):
        aad_app.validate_sql(sql, {"vw_oee_master"})


def test_requires_an_authorized_source_even_for_select_literal(aad_app):
    with pytest.raises(ValueError, match="vista autorizada"):
        aad_app.validate_sql("SELECT 1 AS valor", {"vw_oee_master"})


def test_rejects_invalid_sql_syntax(aad_app):
    with pytest.raises(ValueError, match="GoogleSQL válido"):
        aad_app.validate_sql("SELECT FROM", {"vw_oee_master"})


def test_validates_exact_production_week(aad_app):
    history = []
    assert aad_app.business_sql_error(
        "OEE RB4 semana 2026-22", history,
        "SELECT semana FROM `aad-analitica-oee.oee_planta.vw_oee_master` WHERE semana = '2026-22'",
    ) is None
    error = aad_app.business_sql_error(
        "OEE RB4 semana 2026-22", history,
        "SELECT fecha FROM `aad-analitica-oee.oee_planta.vw_oee_master` WHERE fecha = DATE '2026-05-25'",
    )
    assert "semana = '2026-22'" in error


def test_validates_planner_need_week_column(aad_app):
    sql = "SELECT semana_necesidad FROM `aad-analitica-oee.oee_planta.vw_planificador_capacidad`"
    assert aad_app.business_sql_error("carga pendiente semana 38", [], sql) is None
    wrong = "SELECT semana FROM `aad-analitica-oee.oee_planta.vw_planificador_capacidad`"
    assert "semana_necesidad" in aad_app.business_sql_error("carga pendiente semana 38", [], wrong)


def test_validates_long_stop_consolidation_before_limit(aad_app):
    good = """SELECT id_parada, SUM(tiempo_parada_min) FROM
`aad-analitica-oee.oee_planta.vw_import_paradas` GROUP BY id_parada ORDER BY 2 DESC LIMIT 5"""
    assert aad_app.business_sql_error("cinco paradas más largas", [], good) is None
    bad = "SELECT id_parada, tiempo_parada_min FROM `aad-analitica-oee.oee_planta.vw_import_paradas` ORDER BY tiempo_parada_min DESC LIMIT 5"
    assert "consolidar los tramos" in aad_app.business_sql_error("cinco paradas más largas", [], bad)
