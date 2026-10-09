"""Shared fixtures; Google clients are replaced before importing app.py."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session")
def aad_app():
    """Import application logic while making cloud access impossible in tests.

    Constructors and ADC are patched before module import because app.py creates
    Google clients at module scope. These inert objects do not implement network
    methods, so an accidental service call fails locally instead of reaching GCP.
    """
    import google.auth
    from google import genai
    from google.cloud import bigquery, firestore

    inert = SimpleNamespace()
    google.auth.default = lambda **_kwargs: (inert, "test-project")
    bigquery.Client = lambda **_kwargs: inert
    firestore.Client = lambda **_kwargs: inert
    genai.Client = lambda **_kwargs: inert

    module = importlib.import_module("app")
    yield module
    importlib.sys.modules.pop("app", None)


@pytest.fixture
def synthetic_oee_rows():
    """Synthetic aggregate: 900 good, 100 rework, 0.5 OEE, €1,000 loss."""
    return [{
        "articulo": "SYNTH-001",
        "maquina": "RB4",
        "periodo": "fixture sintético",
        "total_piezas_producidas": 1000,
        "oee_actual_pct": 0.5,
        "oee_objetivo_pct": 0.65,
        "perdida_actual_oee_eur": 1000.0,
        "ahorro_estimado_eur": 300.0,
        "perdida_restante_estimada_eur": 700.0,
    }]


@pytest.fixture
def history_rb4_week22():
    return [{
        "user": "Dame el OEE de RB4 en la semana 2026-22.",
        "assistant": "RB4 en semana 2026-22.",
    }]
