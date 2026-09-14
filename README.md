# Chatbot AAD para Google Chat

Aplicación interna de Google Chat que convierte preguntas de negocio en consultas de solo lectura
sobre `vw_oee_master`, `vw_import_paradas`, `vw_planificador_capacidad` y
`vw_sugerencias_balanceo` del dataset
`aad-analitica-oee.oee_planta`. Reconoce expresiones habituales como cartera, backlog, OF, PEV,
referencia, equipo, tiempo muerto, saturación o reparto de carga sin exigir nombres técnicos.

Las sugerencias de balanceo se interpretan sobre una capacidad de 15 turnos semanales por robot.
El asistente distingue entre los turnos liberados en el origen y los turnos ajustados por OEE que
necesita el destino, por lo que no confunde una solución de capacidad con una mejora de eficiencia.

La versión v79 añade una interpretación semántica previa en lenguaje natural para tolerar el
vocabulario libre de los operarios, errores, abreviaturas, fechas informales y preguntas abiertas.
La pregunta original se conserva para la conversación y la reformulación se utiliza únicamente
para construir una consulta segura. Si la primera interpretación no se puede consultar, se realiza
un segundo intento antes de solicitar una aclaración.

Las preguntas operativas abiertas con un periodo concreto obtienen un diagnóstico por robot con
producción, reoperaciones, OEE, componentes y pérdida económica. Un periodo nuevo corta filtros
antiguos que no estén mencionados, y las búsquedas por prefijo de artículo se identifican como
prefijos también en la trazabilidad.
Las preguntas que introducen un periodo completo nuevo cortan además las máquinas y artículos
heredados, evitando que la trazabilidad de planificación arrastre filtros de análisis anteriores.
Los rankings explícitos por máquina se construyen de forma determinista con la métrica solicitada,
y las fechas relativas resueltas por código se imponen al intérprete semántico para evitar que el
modelo recalcule erróneamente expresiones como «viernes pasado».

Las consultas globales de planificación aíslan el historial de filtros concretos anteriores,
evitando que una máquina o fecha se arrastre accidentalmente a un ranking de referencias o carga
pendiente.

La versión conserva de forma explícita el alcance temporal histórico en las continuaciones,
impide introducir fechas diarias que el usuario no haya solicitado y añade al final de cada
respuesta de datos un bloque breve «Cómo se ha calculado». Las simulaciones de ahorro por objetivo
de OEE utilizan una hipótesis proporcional declarada y no se presentan como ahorros garantizados.
Además, las preguntas sobre el alcance mantenido devuelven los filtros concretos en vez del menú de
capacidades, y expresiones como «dame más detalles sin cambiar el periodo» reutilizan de forma
determinista la máquina y la fecha anteriores.
«Último trimestre» se resuelve al trimestre natural cerrado anterior y conserva sus fechas exactas.
Las preguntas de seguimiento sobre las fechas del trimestre vuelven a mostrar siempre el intervalo
completo, incluso si la memoria conversacional está resumida. También resuelve meses explícitos
(por ejemplo, «paradas del mes de agosto») como el intervalo completo de ese mes y sustituye el
filtro temporal anterior.
en las consultas y en las respuestas de seguimiento. También reconoce preguntas como «¿qué fechas
has usado?» y responde con el intervalo concreto en lugar de derivarlas al menú de capacidades.

## Variables de entorno

- `GOOGLE_CLOUD_PROJECT`: `aad-analitica-oee`
- `BIGQUERY_DATASET`: `oee_planta`
- `GOOGLE_CLOUD_LOCATION`: región de Vertex AI y BigQuery
- `BIGQUERY_LOCATION`: ubicación del dataset, por ejemplo `EU`; si se omite, BigQuery la detecta
- `MODEL_ID`: modelo Gemini disponible, por defecto `gemini-2.5-flash`
- `MAXIMUM_BYTES_BILLED`: límite por consulta, por defecto 1 GB
- `MAX_RESULT_ROWS`: máximo de filas entregadas al modelo, por defecto 100
- `BLOCKED_SOURCE_TABLES`: fuentes adicionales bloqueadas incluso mediante vistas; vacío por defecto. Las tablas externas se bloquean automáticamente.
- `ALLOWED_VIEWS`: lista cerrada de vistas que el chatbot puede consultar
- `MEMORY_COLLECTION`: colección de Firestore para la memoria; por defecto `chatbot_aad_conversations`
- `MAX_MEMORY_TURNS`: intercambios recientes conservados por conversación; por defecto 8

## Ejecución local

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:GOOGLE_CLOUD_PROJECT="aad-analitica-oee"
$env:BIGQUERY_DATASET="oee_planta"
$env:GOOGLE_CLOUD_LOCATION="europe-west1"
.venv\Scripts\python app.py
```

La autenticación local utiliza las credenciales predeterminadas de Google Cloud. En Cloud Run se utiliza la cuenta de servicio asignada al servicio.

La memoria conversacional usa Firestore y permite resolver expresiones como “esa misma semana” o “la misma máquina”. La cuenta `chatbot-aad` necesita el rol `roles/datastore.user` y el proyecto debe tener una base de datos Firestore en modo Native.

El servicio no solicita acceso a Google Drive. Las vistas autorizadas deben depender únicamente de fuentes nativas de BigQuery.

## Despliegue previsto

El servicio debe ejecutarse con la cuenta `chatbot-aad` y sin acceso público. Se concede a `chat@system.gserviceaccount.com` el rol de invocador de Cloud Run y se configura la URL resultante como endpoint HTTP de Google Chat.
