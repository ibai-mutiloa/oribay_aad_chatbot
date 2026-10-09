from types import SimpleNamespace


def test_period_only_oee_follow_up_preserves_machine_and_replaces_week(aad_app, monkeypatch):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40.",
        "assistant": "RB7: disponibilidad calculada para la semana 2026-40.",
    }]
    prompt_seen = []

    def fake_generate_content(*, model, contents, config):
        prompt_seen.append(contents)
        return SimpleNamespace(text="Disponibilidad de RB7 en la semana 2026-41")

    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content)),
    )
    rewritten = aad_app.semantic_rewrite_question("Ahora en la semana 2026-41", "schema test", history)
    filters = aad_app.active_filters(rewritten, history)
    sql = aad_app.deterministic_single_kpi_sql(rewritten, history)

    assert "Disponibilidad de RB7 en la semana 2026-40" in prompt_seen[0]
    assert filters["maquina"] == "RB7"
    assert filters["semana"] == "2026-41"
    assert "semana = '2026-41'" in sql
    assert "semana = '2026-40'" not in sql
    assert "UPPER(TRIM(maquina)) = 'RB7'" in sql


def test_production_regression_gemini_cannot_replace_availability_with_global_oee(
    aad_app, monkeypatch
):
    history = [{
        "user": "¿Cuál fue la disponibilidad de la máquina RB7 en la semana 2026-40?",
        "assistant": "La disponibilidad de la máquina RB7 en la semana 2026-40 fue del 83,10%.",
    }]
    prompt_seen = []

    def fake_generate_content(*, model, contents, config):
        prompt_seen.append(contents)
        # Deliberately reproduce the incorrect production rewrite.
        return SimpleNamespace(text="¿Cuál es el OEE de la semana 2026-39?")

    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content)),
    )
    original = "¿Y en la semana 2026-39?"
    interpreted = aad_app.semantic_rewrite_question(original, "schema test", history)
    filters = aad_app.active_filters(interpreted, history)
    sql = aad_app.make_sql(interpreted, "schema test", history)

    assert "disponibilidad" in interpreted.lower()
    assert "RB7" in interpreted
    assert filters["maquina"] == "RB7"
    assert filters["semana"] == "2026-39"
    assert "2026-40" not in interpreted
    assert "semana = '2026-39'" in sql
    assert "semana = '2026-40'" not in sql
    assert "UPPER(TRIM(maquina)) = 'RB7'" in sql
    assert "disponibilidad_pct" in sql
    assert "OEE de la semana" not in interpreted
    assert prompt_seen


def test_explicit_oee_change_is_kept_despite_wrong_gemini_metric(aad_app, monkeypatch):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40",
        "assistant": "Disponibilidad RB7 83,10%",
    }]
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="Disponibilidad de RB7 en la semana 2026-39"
        ))),
    )
    question = "Ahora el OEE de RB7 en la semana 2026-39"
    interpreted = aad_app.semantic_rewrite_question(question, "schema test", history)
    assert "OEE" in interpreted
    assert "disponibilidad" not in interpreted.lower()


def test_explicit_machine_change_is_not_lost_by_gemini(aad_app, monkeypatch):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40",
        "assistant": "Disponibilidad RB7 83,10%",
    }]
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="Disponibilidad de RB7 en la semana 2026-40"
        ))),
    )
    question = "¿Y RB8?"
    interpreted = aad_app.semantic_rewrite_question(question, "schema test", history)
    filters = aad_app.active_filters(interpreted, history)
    assert "Disponibilidad" in interpreted
    assert "RB8" in interpreted
    assert filters["maquina"] == "RB8"
    assert filters["semana"] == "2026-40"


def test_period_follow_up_preserves_comparison_metric_and_both_machines(aad_app, monkeypatch):
    history = [{
        "user": "Compara la disponibilidad de RB7 y RB8 en la semana 2026-40",
        "assistant": "Comparación de disponibilidad RB7/RB8 en semana 2026-40",
    }]
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="OEE global de la semana 2026-39"
        ))),
    )
    interpreted = aad_app.semantic_rewrite_question(
        "¿Y en la semana 2026-39?", "schema test", history
    )
    filters = aad_app.active_filters(interpreted, history)
    assert "Comparar" in interpreted
    assert "disponibilidad" in interpreted.lower()
    assert "RB7" in interpreted and "RB8" in interpreted
    assert filters["maquinas"] == "RB7,RB8"
    assert "maquina" not in filters
    assert filters["semana"] == "2026-39"


def test_explicit_all_machines_scope_is_not_lost_by_gemini(aad_app, monkeypatch):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40",
        "assistant": "Disponibilidad RB7 83,10%",
    }]
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="Disponibilidad de RB7 en la semana 2026-39"
        ))),
    )
    question = "Ahora todas las máquinas en la semana 2026-39"
    interpreted = aad_app.semantic_rewrite_question(question, "schema test", history)
    filters = aad_app.active_filters(interpreted, history)
    assert interpreted == question
    assert "maquina" not in filters and "maquinas" not in filters
    assert filters["semana"] == "2026-39"


def test_period_follow_up_without_history_does_not_gain_filters_from_rewrite(aad_app, monkeypatch):
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="¿Cuál es el OEE de RB7 en la semana 2026-39?"
        ))),
    )
    question = "¿Y en la semana 2026-39?"
    interpreted = aad_app.semantic_rewrite_question(question, "schema test", [])
    filters = aad_app.active_filters(interpreted, [])
    assert interpreted == question
    assert "maquina" not in filters
    assert aad_app.deterministic_single_kpi_sql(interpreted, []) is None


def test_explicit_domain_change_is_not_rewritten_as_oee_or_inherits_machine(aad_app, monkeypatch):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40",
        "assistant": "Disponibilidad RB7 83,10%",
    }]
    monkeypatch.setattr(
        aad_app, "ai",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(
            text="OEE de RB7 en la semana 2026-39"
        ))),
    )
    question = "Paradas en la semana 2026-39"
    interpreted = aad_app.semantic_rewrite_question(question, "schema test", history)
    filters = aad_app.active_filters(interpreted, history)
    assert interpreted == question
    assert filters["modo_consulta"] == "paradas"
    assert filters["semana"] == "2026-39"
    assert "maquina" not in filters


def test_multi_machine_comparison_survives_period_change_without_singular_machine(aad_app):
    history = [{
        "user": "Compara la disponibilidad de RB7 y RB8 en la semana 2026-40.",
        "assistant": "RB7 y RB8 comparados para semana 2026-40.",
    }]
    filters = aad_app.active_filters("Ahora en la semana 2026-41", history)
    assert filters["maquinas"] == "RB7,RB8"
    assert "maquina" not in filters
    assert filters["semana"] == "2026-41"


def test_production_week_to_natural_week_keeps_machine_and_replaces_temporal_kind(aad_app):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40.",
        "assistant": "RB7, semana 2026-40.",
    }]
    natural_period = aad_app.resolve_temporal_context("semana pasada")
    filters = aad_app.active_filters("semana pasada", history)
    sql = aad_app.deterministic_single_kpi_sql(
        "Disponibilidad de RB7 en la semana pasada", history
    )
    assert filters["maquina"] == "RB7"
    assert filters["fecha_desde"] == natural_period["fecha_desde"]
    assert filters["fecha_hasta"] == natural_period["fecha_hasta"]
    assert "semana" not in filters
    assert f"fecha BETWEEN DATE '{natural_period['fecha_desde']}' AND DATE '{natural_period['fecha_hasta']}'" in sql
    assert "semana = '2026-40'" not in sql


def test_explicit_all_machines_clears_robot_but_keeps_period(aad_app):
    history = [{
        "user": "OEE de RB7 en la semana 2026-40.",
        "assistant": "RB7 semana 2026-40.",
    }]
    filters = aad_app.active_filters("Ahora todas las máquinas", history)
    assert "maquina" not in filters
    assert "maquinas" not in filters
    assert filters["semana"] == "2026-40"


def test_article_change_keeps_compatible_machine_and_period(aad_app):
    history = [{
        "user": "OEE del artículo AAAA000001 en RB7, semana 2026-40.",
        "assistant": "Artículo AAAA000001 en RB7, semana 2026-40.",
    }]
    question = "OEE del artículo BBBB000002"
    filters = aad_app.active_filters(question, history)
    sql = aad_app.deterministic_single_kpi_sql(
        "OEE del artículo BBBB000002 en RB7, semana 2026-40", history
    )
    assert filters["articulo"] == "BBBB000002"
    assert filters["maquina"] == "RB7"
    assert filters["semana"] == "2026-40"
    assert "UPPER(TRIM(articulo)) = 'BBBB000002'" in sql
    assert "UPPER(TRIM(articulo)) = 'AAAA000001'" not in sql
    assert "semana = '2026-40'" in sql


def test_stops_week_change_preserves_machine_and_uses_new_week_sql(aad_app):
    history = [{
        "user": "Resumen de paradas de RB7 en la semana 2026-40.",
        "assistant": "Resumen de RB7 para la semana 2026-40.",
    }]
    question = "Resumen de paradas en la semana 2026-41"
    filters = aad_app.active_filters(question, history)
    sql = aad_app.deterministic_stop_summary_sql(question, history)
    assert filters["modo_consulta"] == "paradas"
    assert filters["maquina"] == "RB7"
    assert filters["semana"] == "2026-41"
    assert "UPPER(TRIM(maquina)) = 'RB7'" in sql
    assert "semana = '2026-41'" in sql
    assert "semana = '2026-40'" not in sql


def test_planning_need_week_change_preserves_machine(aad_app):
    history = [{
        "user": "Carga pendiente de RB7 en semana 40.",
        "assistant": "RB7 semana 40.",
    }]
    filters = aad_app.active_filters("Carga pendiente en semana 41", history)
    sql = aad_app.deterministic_planner_week_load_sql("Carga pendiente en semana 41", history)
    assert filters["modo_consulta"] == "planificador"
    assert filters["maquina"] == "RB7"
    assert filters["semana"].endswith("-41")
    assert "UPPER(TRIM(maquina)) = 'RB7'" in sql
    assert "CAST(semana_necesidad AS STRING) IN ('41')" in sql
    assert "semana_necesidad AS STRING) IN ('40')" not in sql


def test_balance_week_change_keeps_explicit_iso_year_and_other_filters(aad_app):
    history = [{
        "user": "Sugerencias de balanceo para RB7, artículo AAAA000001, semana 2026-40.",
        "assistant": "Propuesta para RB7, artículo AAAA000001, semana 2026-40.",
    }]
    question = "Sugerencias de balanceo para semana 2026-41"
    filters = aad_app.active_filters(question, history)
    sql = aad_app.deterministic_balance_suggestions_sql(question, history)
    assert filters["maquina"] == "RB7"
    assert filters["articulo"] == "AAAA000001"
    assert filters["semana"] == "2026-41"
    assert "EXTRACT(ISOYEAR FROM fecha) = 2026" in sql
    assert "SAFE_CAST(semana AS INT64) = 41" in sql
    assert "SAFE_CAST(semana AS INT64) = 40" not in sql
    assert "UPPER(TRIM(maquina_origen)) = 'RB7'" in sql
    assert "UPPER(TRIM(articulo)) = 'AAAA000001'" in sql


def test_scope_reconciliation_without_history(aad_app):
    filters = aad_app.active_filters("Disponibilidad de RB7 en la semana 2026-41", [])
    sql = aad_app.deterministic_single_kpi_sql(
        "Disponibilidad de RB7 en la semana 2026-41", []
    )
    assert filters["maquina"] == "RB7"
    assert filters["semana"] == "2026-41"
    assert "semana = '2026-41'" in sql


def test_ambiguous_machine_history_requests_clarification_instead_of_selecting_one(aad_app):
    history = [{
        "user": "¿Disponibilidad de RB7 o RB8 en la semana 2026-40?",
        "assistant": "Puedo revisar ambas máquinas.",
    }]
    question = "Ahora en la semana 2026-41"
    filters = aad_app.active_filters(question, history)
    sql_or_clarification = aad_app.make_sql(question, "schema test", history)
    assert filters["requiere_aclaracion"] == "maquinas"
    assert filters["alternativas_maquina"] == "RB7,RB8"
    assert "maquina" not in filters
    assert sql_or_clarification.startswith("ACLARAR:")
    assert "RB7" in sql_or_clarification and "RB8" in sql_or_clarification


def test_domain_switch_does_not_inherit_incompatible_machine_or_period(aad_app):
    history = [{
        "user": "Disponibilidad de RB7 en la semana 2026-40.",
        "assistant": "RB7, semana 2026-40.",
    }]
    filters = aad_app.active_filters("Paradas en la semana 2026-41", history)
    assert filters["modo_consulta"] == "paradas"
    assert filters["semana"] == "2026-41"
    assert "maquina" not in filters


def test_planning_and_balance_use_separate_context_domains(aad_app):
    history = [{
        "user": "Sugerencias de balanceo para RB7, artículo AAAA000001, semana 2026-40.",
        "assistant": "Propuesta para RB7, artículo AAAA000001, semana 2026-40.",
    }]
    filters = aad_app.active_filters("Carga pendiente en semana 41", history)
    assert filters["modo_consulta"] == "planificador"
    assert filters["semana"].endswith("-41")
    assert "maquina" not in filters
    assert "articulo" not in filters
