# Pruebas locales de regresión

Ejecutar desde la raíz:

```bash
python -m pytest
```

`tests/conftest.py` sustituye ADC y constructores de BigQuery, Firestore y Gemini antes de importar `app.py`. Los tests cubren funciones locales y SQL generado; no usan servicios externos. Los datos de `synthetic_oee_rows` son ficticios y están etiquetados en el fixture. La prueba `xfail` registra el defecto conocido del filtro anual de sugerencias de balanceo.
