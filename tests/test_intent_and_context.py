def test_extracts_machine_article_and_production_week(aad_app):
    filters = aad_app.active_filters("OEE de RB 8 para artículo 24DMRUB0D06 en semana 2026-33", [])
    assert filters["maquina"] == "RB8"
    assert filters["articulo"] == "24DMRUB0D06"
    assert filters["semana"] == "2026-33"


def test_prefix_is_kept_as_prefix_filter(aad_app):
    filters = aad_app.active_filters("enséñame las referencias que empiezan por 220", [])
    assert filters["articulo_prefijo"] == "220"
    assert "articulo" not in filters


def test_two_explicit_machines_are_both_retained(aad_app):
    filters = aad_app.active_filters("Compara RB6 y RB7 en 2026-22", [])
    assert filters["maquinas"] == "RB6,RB7"
    assert filters["semana"] == "2026-22"


def test_follow_up_inherits_machine_and_week(aad_app, history_rb4_week22):
    filters = aad_app.active_filters("Dame más detalles sin cambiar el periodo.", history_rb4_week22)
    assert filters["maquina"] == "RB4"
    assert filters["semana"] == "2026-22"


def test_new_explicit_period_preserves_machine_in_same_domain(aad_app, history_rb4_week22):
    filters = aad_app.active_filters("¿Cuál fue el OEE el 4 de septiembre de 2026?", history_rb4_week22)
    assert filters["fecha_desde"] == filters["fecha_hasta"] == "2026-09-04"
    assert filters["maquina"] == "RB4"


def test_follow_up_ranking_retains_period_but_not_machine_scope(aad_app, history_rb4_week22):
    filters = aad_app.active_filters("¿Cuál fue la peor máquina en rendimiento?", history_rb4_week22)
    sql = aad_app.deterministic_machine_metric_ranking_sql(
        "¿Cuál fue la peor máquina en rendimiento?", history_rb4_week22
    )
    assert filters["semana"] == "2026-22"
    assert "semana = '2026-22'" in sql
    assert "UPPER(TRIM(maquina)) = 'RB4'" not in sql


def test_query_mode_changes_from_stops_to_explicit_kpi(aad_app):
    history = [{"user": "Resume las paradas de RB4", "assistant": "..."}]
    assert aad_app.query_mode("¿Qué calidad tuvo RB4?", history) == "global"


def test_contextual_planner_weeks_resolve_demonstrative_follow_up(aad_app):
    history = [{"user": "Compara la carga de las semanas 38 y 39", "assistant": "..."}]
    assert aad_app.planner_context_weeks("¿Qué equipos están más saturados esas semanas?", history) == [38, 39]
