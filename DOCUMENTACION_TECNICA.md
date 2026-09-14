# Documentación técnica — Asistente OEE - AAD

## 1. Propósito y alcance

Aplicación interna que recibe preguntas en español desde Google Chat, genera consultas GoogleSQL de solo lectura y devuelve respuestas sobre OEE, producción y paradas.

El chatbot consulta exclusivamente:

- `aad-analitica-oee.oee_planta.vw_oee_master`: producción e indicadores OEE.
- `aad-analitica-oee.oee_planta.vw_import_paradas`: incidencias y paradas.
- `aad-analitica-oee.oee_planta.vw_planificador_capacidad`: carga pendiente y planificación por artículo y máquina.
- `aad-analitica-oee.oee_planta.vw_sugerencias_balanceo`: propuestas finales de balanceo ya calculadas.

No necesita acceso a Google Drive. Las vistas autorizadas deben depender de fuentes nativas de BigQuery y no de tablas externas bloqueadas.

## 2. Arquitectura

```text
Usuario de Google Chat
        |
        v
Google Chat API / Workspace Add-on
        |
        v
Cloud Run: chatbot-aad (Flask + Gunicorn)
        |
        +--> Firestore: memoria de conversación
        +--> Vertex AI: Gemini 2.5 Flash
        +--> BigQuery: SQL validado
                +--> vw_oee_master
                +--> vw_import_paradas
```

## 3. Servicios y dependencias

| Servicio | Responsabilidad | Dependencia |
|---|---|---|
| Google Chat API | Entrega eventos `MESSAGE` y recibe respuestas | Endpoint HTTPS y permiso `roles/run.invoker` |
| Cloud Run | Ejecuta el contenedor | Cuenta de servicio de ejecución |
| Vertex AI | Interpreta preguntas, genera SQL y redacta respuestas | API habilitada y `roles/aiplatform.user` |
| BigQuery | Lee esquema y ejecuta consultas | `roles/bigquery.jobUser` y `roles/bigquery.dataViewer` |
| Firestore Native | Guarda contexto conversacional | Base creada y `roles/datastore.user` |

Dependencias Python fijadas en `requirements.txt`:

```text
Flask==3.1.2
gunicorn==23.0.0
google-cloud-bigquery==3.36.0
google-genai==1.31.0
google-auth==2.40.3
requests==2.32.5
google-cloud-firestore==2.21.0
sqlglot==27.14.0
```

## 4. Archivos

- `app.py`: aplicación completa, endpoint, memoria, reglas, SQL y redacción.
- `requirements.txt`: dependencias Python.
- `Dockerfile`: imagen Python 3.12 y servidor Gunicorn.
- `.dockerignore`: exclusiones de construcción.
- `README.md`: configuración y despliegue básico.
- `eval/`: pruebas conversacionales.

## 5. Configuración

| Variable | Valor habitual | Función |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | `aad-analitica-oee` | Proyecto GCP |
| `BIGQUERY_DATASET` | `oee_planta` | Dataset |
| `GOOGLE_CLOUD_LOCATION` | `europe-west1` | Región de Vertex AI |
| `BIGQUERY_LOCATION` | opcional | Ubicación explícita de BigQuery |
| `MODEL_ID` | `gemini-2.5-flash` | Modelo Gemini |
| `MAXIMUM_BYTES_BILLED` | `1000000000` | Límite de bytes por consulta |
| `MAX_RESULT_ROWS` | `100` | Máximo de filas devueltas |
| `ALLOWED_VIEWS` | `vw_import_paradas,vw_oee_master` | Allowlist de vistas |
| `BLOCKED_SOURCE_TABLES` | `raw_ordenes_pendientes` | Fuentes bloqueadas |
| `MEMORY_COLLECTION` | `chatbot_aad_conversations` | Colección Firestore |
| `MAX_MEMORY_TURNS` | `8` | Turnos conservados |
| `SCHEMA_CACHE_SECONDS` | `600` | Caché de esquema |
| `ENABLE_PROGRESS_MESSAGE` | `true` | Mensaje provisional de progreso |

## 6. Flujo de una petición

1. `chat_event()` recibe el evento y extrae la pregunta.
2. `conversation_id()` identifica el espacio y el usuario.
3. `load_history()` recupera los últimos turnos desde Firestore.
4. `resolve_temporal_context()` resuelve fechas relativas con zona `Europe/Madrid`.
5. `active_filters()` recupera máquina, artículo, semana, fecha y modo.
6. Se publica “Consultando datos…” si está habilitado el progreso.
7. `dataset_schema()` obtiene vistas, columnas y dependencias autorizadas.
8. `make_sql()` selecciona SQL determinista o solicita SQL a Gemini.
9. `validate_sql()` bloquea escrituras, sentencias múltiples y tablas no autorizadas.
10. `business_sql_error()` valida fórmulas y semántica.
11. `execute_query()` hace dry-run, controla bytes y ejecuta BigQuery.
12. `explain()` redacta la respuesta usando únicamente los resultados.
13. `google_chat_text()` adapta Markdown al formato de Google Chat.
14. Se actualiza el mensaje provisional y se guarda el turno.

## 7. Funciones clave

### Entrada, salida y formato

- `chat_event()`: endpoint `POST /` para Google Chat y Chat API.
- `health()`: endpoint `GET /` de comprobación de salud.
- `chat_response()`: envelope de respuesta.
- `create_progress_message()`: crea el mensaje provisional.
- `update_progress_message()`: sustituye el mensaje provisional.
- `final_chat_response()`: actualiza el provisional o responde directamente.
- `google_chat_text()`: convierte `**texto**` en `*texto*` y elimina encabezados Markdown.

### Memoria

- `conversation_id()`: crea un identificador aislado por conversación.
- `load_history()` y `save_turn()`: lectura y escritura de Firestore.
- `clear_history()`: borra contexto con “Reiniciar memoria”.
- `context_question_answer()`: responde sobre periodo y filtros activos.
- `capability_response()`: responde sobre alcance, capacidades, informes y funcionamiento general sin consultar SQL.

La memoria almacena preguntas y respuestas recientes, no datos de producción.

### Interpretación

- `resolve_temporal_context()`: entiende `ayer`, `viernes pasado`, `semana pasada`, `semana anterior`, fechas y semanas.
- `query_mode()`: clasifica `global`, `por_articulo`, `paradas`, `ranking` o `global_y_por_articulo`.
- `active_filters()`: combina mensaje actual e historial; el mensaje actual tiene prioridad.

### Esquema y seguridad

- `_blocked_view()`: detecta dependencias directas o indirectas con fuentes bloqueadas.
- `dataset_schema()`: construye el esquema enviado a Gemini y la allowlist.
- `extract_sql()`: extrae SQL de respuestas de Gemini.
- `validate_sql()`: valida sintaxis, lectura y nombres autorizados.
- `business_sql_error()`: valida semanas exactas, pérdidas y deduplicación.

### Generación SQL

- `deterministic_stop_summary_sql()`: resumen por máquina con tramos consolidados por `id_parada`.
- `deterministic_top_stops_sql()`: ranking de paradas largas después de sumar sus tramos.
- `deterministic_stop_details_sql()`: detalle de paradas, averías o tiempos muertos consolidado
  por `id_parada` y con el periodo operativo exacto.
- `deterministic_single_kpi_sql()`: KPI de una máquina y periodo.
- `deterministic_planner_week_load_sql()`: carga pendiente por robot y semana de necesidad.
- `planner_context_weeks()`: conserva listas de semanas en continuaciones como «esas semanas».
- `deterministic_planner_candidate_sql()`: candidatos para un artículo y robot de origen, también
  en continuaciones como «compara la experiencia, OEE y carga de esos candidatos».
- `deterministic_balance_suggestions_sql()`: consulta directa de las sugerencias calculadas de balanceo.

### Interpretación del balanceo semanal

La capacidad de referencia es de 15 turnos semanales por robot. En
`vw_sugerencias_balanceo`, `turnos_origen_liberados` representa la carga retirada de la máquina
sobrecargada y `turnos_destino_nuevos` representa los turnos ajustados por OEE que necesitará el
destino. Ambas cifras pueden ser diferentes porque cada robot puede tener distinta cadencia y OEE
para el mismo artículo. Una propuesta puede resolver la sobrecarga semanal aunque consuma más
turnos totales; en ese caso se presenta como balanceo de capacidad y no como mejora de eficiencia.
- `planner_historical_oee_ctes()`: cálculo corporativo común del OEE histórico de candidatos.
- `make_sql()`: aplica primero las consultas deterministas y después Gemini.
- `repair_sql()`: intenta corregir errores SQL o de reglas.
- `execute_query()`: dry-run, límite de bytes y ejecución real.
- `explain()`: respuesta final en español.

## 8. Reglas de negocio

En `vw_oee_master`:

- Calidad = `SUM(buenas) / SUM(buenas + reoperar)`.
- Disponibilidad = `SUM(tiempo_erp_capado) / SUM(tiempo_plan)`.
- Rendimiento = `SUM(buenas + reoperar) / SUM(piezas_teo)`.
- OEE = Calidad × Disponibilidad × Rendimiento.
- Piezas producidas = buenas + reoperar.
- Coste corporativo de pérdida = `24,9 €/hora`.

En `vw_import_paradas`:

- `oee = 'SI'`: OEE, no planificada o incidencia.
- `oee = 'NO'`: No OEE, planificada.
- `fecha_operativa` ya usa el corte de turno de las 06:00.
- Una incidencia puede contener varios tramos con el mismo `id_parada`; los resúmenes, detalles y
  rankings los agrupan y suman antes de calcular totales u ordenar.

En `vw_planificador_capacidad`:

- `cantidad_pendiente` representa la carga pendiente de la línea de planificación.
- `horas_totales_aplicadas` y `turnos_aplicados` representan la carga asignada. En la respuesta
  al usuario, `turnos_aplicados` se presenta como «turnos ajustados por OEE».
- `horas_desviacion` y `turnos_desviacion` sirven para detectar desviaciones de carga.
- La desviación agregada de turnos se recalcula como `SUM(turnos_aplicados) -
  SUM(turnos_teoricos_erp)`.
- `fecha_necesidad` y `semana_necesidad` representan la necesidad del cliente.
- `oee_aplicado` no sustituye al OEE calculado en `vw_oee_master`.
- Las semanas solicitadas al planificador se filtran con `semana_necesidad` y el año con
  `EXTRACT(YEAR FROM fecha_necesidad)`.
- Un robot candidato debe tener evidencia en `vw_oee_master` de haber fabricado el mismo artículo.
  No se exige un volumen mínimo, un OEE superior ni una desviación inferior.
- La experiencia, el OEE histórico y la carga del candidato se muestran como elementos para que el
  usuario valore la propuesta, no como filtros obligatorios.
- Los candidatos se clasifican como `FAVORABLE_EN_DATOS`, `MIXTO`,
  `DESFAVORABLE_EN_DATOS` o `COMPATIBLE_SIN_CARGA_REGISTRADA`. La clasificación compara OEE
  histórico y desviación global, pero no sustituye la validación técnica.
- En cada línea se priorizan candidatos favorables, después mixtos, compatibles sin carga
  registrada y finalmente desfavorables; ninguno se elimina por la clasificación.
- Los análisis generales de redistribución se limitan a 10 candidatos para evitar respuestas
  incompletas por longitud.
- Si no existe otro robot con experiencia en el artículo, el asistente no inventa una alternativa.
- Las redistribuciones son recomendaciones de solo lectura y candidatos para validación; no
  modifican planes ni acreditan por sí solas compatibilidad de utillaje o capacidad libre real.

Para `semana 2026-22` se usa `semana = '2026-22'`; no se transforma en un intervalo aproximado de fechas.

## 9. Permisos

Cuenta de ejecución de Cloud Run:

`chatbot-aad@aad-analitica-oee.iam.gserviceaccount.com`

Permisos necesarios:

- `roles/bigquery.jobUser`.
- `roles/bigquery.dataViewer` sobre el proyecto o dataset.
- `roles/datastore.user`.
- `roles/aiplatform.user`.

La cuenta de servicio del complemento de Google Chat debe tener `roles/run.invoker` sobre Cloud Run. El servicio se despliega con `--no-allow-unauthenticated`.

## 10. Despliegue

```bash
unzip -o chatbot-aad-memoria-globales-v27.zip

gcloud run deploy chatbot-aad \
  --source . \
  --project aad-analitica-oee \
  --region europe-west1 \
  --service-account chatbot-aad@aad-analitica-oee.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --set-env-vars BIGQUERY_DATASET=oee_planta,GOOGLE_CLOUD_LOCATION=europe-west1,MODEL_ID=gemini-2.5-flash,MAXIMUM_BYTES_BILLED=1000000000
```

## 11. Pruebas

```bash
curl -i https://chatbot-aad-809725501359.europe-west1.run.app/
```

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{"type":"MESSAGE","message":{"text":"Dame el OEE de RB4 en la semana 2026-22."}}' \
  https://chatbot-aad-809725501359.europe-west1.run.app
```

La batería de `eval/run_eval.py` cubre KPI, rankings, paradas, fechas relativas, memoria, pérdidas y fechas futuras. Debe ampliarse con carga por semanas, rankings de desviación y redistribución segura del planificador. Los acumulados anuales deben validarse por formato o recalcularse al actualizar datos.

Logs:

```bash
gcloud run services logs read chatbot-aad \
  --project aad-analitica-oee \
  --region europe-west1 \
  --limit=50
```

## 12. Mantenimiento y limitaciones

- Añadir una vista requiere actualizar `ALLOWED_VIEWS`, `VIEW_BUSINESS_CONTEXT` y revisar dependencias externas.
- El reconocimiento de intención acepta vocabulario funcional sin nombres de tabla: cartera,
  backlog, pedidos u OF/PEV para planificación; pieza/referencia/producto para artículo;
  robot/máquina/equipo para máquina; y avería/tiempo muerto/detención para paradas.
- Cambiar fórmulas requiere modificar `KPI_BUSINESS_RULES`, SQL determinista y evaluaciones.
- Cambiar la clasificación de paradas requiere modificar reglas `SI/NO`, `explain()` y consultas deterministas.
- Cambiar memoria requiere revisar `conversation_id`, `MAX_MEMORY_TURNS` y filtros activos.
- Cambiar modelo requiere revisar región, permisos de Vertex AI y coste.
- Las respuestas dependen de que BigQuery esté actualizado.
- Los acumulados de 2026 cambian al incorporarse nueva producción.
- Google Chat admite un subconjunto de Markdown; la salida se normaliza antes de enviarse.
