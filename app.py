import json
import hashlib
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.api_core.exceptions import BadRequest
from google import genai
from google.cloud import bigquery
from google.cloud import firestore
from google.genai.types import GenerateContentConfig, ThinkingConfig
from sqlglot import exp, parse_one


PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "aad-analitica-oee")
DATASET_ID = os.getenv("BIGQUERY_DATASET", "oee_planta")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
BIGQUERY_LOCATION = os.getenv("BIGQUERY_LOCATION") or None
MODEL_ID = os.getenv("MODEL_ID", "gemini-2.5-flash")
MAXIMUM_BYTES_BILLED = int(os.getenv("MAXIMUM_BYTES_BILLED", "1000000000"))
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "100"))
BLOCKED_SOURCE_TABLES = {
    name.strip().lower()
    for name in os.getenv("BLOCKED_SOURCE_TABLES", "").split(",")
    if name.strip()
}
CONFIGURED_VIEWS = {
    name.strip()
    for name in os.getenv(
        "ALLOWED_VIEWS",
        "vw_import_paradas,vw_oee_master,vw_planificador_capacidad,vw_sugerencias_balanceo",
    ).split(",")
    if name.strip()
}

VIEW_BUSINESS_CONTEXT = {
    "vw_import_paradas": (
        "Vista final del informe de Paradas. Su granularidad es una incidencia. "
        "Contiene duración/tiempo de parada, motivo, clasificación OEE y máquina. "
        "La columna oee usa exactamente SI/NO: SI significa parada no planificada o "
        "incidencia que afecta al OEE; NO significa parada planificada y no imputable como "
        "incidencia OEE. "
        "Sus fechas ya están normalizadas al día o turno operativo con corte a las 06:00; "
        "no se debe volver a desplazar la fecha. Es la fuente preferente para detalles, "
        "recuentos, duración y causas de paradas."
    ),
    "vw_oee_master": (
        "Vista maestra y fuente principal para indicadores OEE. Unifica ERP y Paradas, "
        "incluye el cálculo de capacidad dinámica de la sección manual y ya prorratea el "
        "tiempo de incidencias de cada máquina entre los artículos según sus horas de "
        "fabricación en el turno. No se debe volver a descontar ni prorratear ese tiempo."
    ),
    "vw_planificador_capacidad": (
        "Vista de carga de trabajo y planificación. Su granularidad es una línea de plan "
        "PEV/artículo/máquina. Incluye cantidad pendiente, máquina asignada, fecha y "
        "semana de necesidad, estado, cadencia teórica, OEE real de 7/28/365 días, "
        "OEE aplicado, horas y turnos teóricos, aplicados y desviación. La desviación "
        "de turnos debe calcularse como SUM(turnos_aplicados) menos "
        "SUM(turnos_teoricos_erp). Es la fuente "
        "preferente para analizar carga futura y recomendar redistribuciones. No modifica "
        "planes: solo permite emitir recomendaciones de lectura. Las recomendaciones "
        "deben distinguir datos observados de decisiones propuestas."
    ),
    "vw_sugerencias_balanceo": (
        "Vista final de recomendaciones de balanceo ya calculadas. Contiene fecha, PEV, "
        "artículo, semana, máquina de origen, acción sugerida, piezas a mover, turnos "
        "liberados en origen, destino recomendado, turnos nuevos en destino y otras "
        "opciones compatibles. Es la única fuente para responder preguntas de redistribución "
        "o balanceo; el chatbot no debe volver a calcular ni sustituir sus recomendaciones. "
        "La planificación semanal dispone de un máximo de 15 turnos por robot. Los turnos "
        "nuevos del destino representan turnos ajustados por OEE necesarios para absorber "
        "el movimiento, no una mejora automática de eficiencia."
    ),
}

KPI_BUSINESS_RULES = r"""
FÓRMULAS CORPORATIVAS OBLIGATORIAS (sobre vw_oee_master)

Todas las fórmulas se calculan después de aplicar los filtros solicitados y sobre los
SUM totales del grupo. Nunca calcules estos KPI mediante AVG de porcentajes por fila.

1. calidad_pct:
CASE
  WHEN SUM(buenas + IFNULL(reoperar, 0)) = 0 THEN NULL
  WHEN SUM(buenas) / SUM(buenas + IFNULL(reoperar, 0)) > 1 THEN 1
  ELSE SUM(buenas) / SUM(buenas + IFNULL(reoperar, 0))
END

2. disponibilidad_pct:
CASE
  WHEN SUM(tiempo_plan) = 0 THEN NULL
  ELSE SUM(tiempo_erp_capado) / SUM(tiempo_plan)
END

3. rendimiento_pct:
CASE
  WHEN SUM(piezas_teo) = 0 THEN NULL
  WHEN SUM(buenas + IFNULL(reoperar, 0)) / SUM(piezas_teo) > 1 THEN 1
  ELSE SUM(buenas + IFNULL(reoperar, 0)) / SUM(piezas_teo)
END

4. oee_pct:
calidad_pct * disponibilidad_pct * rendimiento_pct, sustituyendo cada alias por su
expresión completa o calculando los tres componentes en una CTE y multiplicándolos.
Si cualquier componente es NULL, oee_pct es NULL.

5. horas_inactivas:
SUM(tiempo_plan) - SUM(tiempo_erp_capado)

6. piezas_desviadas:
SUM(buenas + reoperar) - SUM(piezas_teo)

7. total_piezas_producidas:
SUM(buenas) + SUM(IFNULL(reoperar, 0))

8. perdida_oee_eur:
((SUM(tiempo_plan) - SUM(tiempo_erp_capado))
 + (SUM(tiempo_erp) - SUM(h_teoricas))
 + (CASE
      WHEN SUM(buenas + reoperar) = 0 THEN 0
      ELSE (SUM(reoperar) / SUM(buenas + reoperar)) * SUM(tiempo_erp)
    END)) * 24.9

9. perdida_rendimiento_eur:
(SUM(tiempo_erp) - SUM(h_teoricas)) * 24.9

10. perdida_calidad_eur:
CASE
  WHEN SUM(buenas + reoperar) = 0 THEN 0
  ELSE (SUM(reoperar) / SUM(buenas + reoperar)) * SUM(tiempo_erp) * 24.9
END

11. perdida_inactividad_eur:
(SUM(tiempo_plan) - SUM(tiempo_erp_capado)) * 24.9

12. tasa_rechazo_pct:
CASE
  WHEN SUM(buenas + reoperar) = 0 THEN NULL
  ELSE SUM(reoperar) / SUM(buenas + reoperar)
END

13. piezas_reoperar:
SUM(IFNULL(reoperar, 0)). Cuando el usuario pida piezas a reoperar,
reoperaciones o retrabajo, devuelve este campo y no lo confundas con piezas buenas.

14. tiempos de ciclo, en segundos por pieza:
tiempo_teorico_real_s = SAFE_DIVIDE(3600 * SUM(tiempo_erp), SUM(piezas_teo))
tiempo_real_s = SAFE_DIVIDE(
  3600 * SUM(tiempo_erp), SUM(buenas + IFNULL(reoperar, 0))
)
Estas expresiones equivalen respectivamente a 3600 / (piezas_teo / tiempo_erp) y
3600 / ((buenas + reoperar) / tiempo_erp). Devuelve NULL cuando el denominador sea cero.
No uses AVG del tiempo de ciclo por fila.

Los campos terminados en _pct se devuelven como proporciones entre 0 y 1. No los
multipliques por 100 en SQL; la respuesta al usuario los presenta como porcentaje.
El coste corporativo es 24.9 EUR por hora y no debe cambiarse ni inferirse otro valor.
"""

GLOBAL_QUERY_RULES = r"""
REGLAS PARA CONSULTAS GLOBALES, COMPARACIONES Y RANKINGS

- "Global", "total" o "de planta" significa agregar todas las filas que cumplan los filtros,
  sin promediar resultados por máquina, artículo, turno, día o semana.
- Cuando el usuario diga "robot", "robots" o "máquinas robot", incluye exclusivamente máquinas
  cuyo nombre normalizado cumpla `REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')`.
  Excluye siempre `MAN` y cualquier otra máquina que no empiece por RB seguida de dígitos.
- Para comparar o clasificar máquinas, artículos, turnos o periodos, calcula cada KPI como
  cociente de SUM dentro de cada grupo. Nunca uses AVG de porcentajes ni AVG de cocientes.
- Excluye de un ranking los grupos cuyo denominador sea cero o cuyo KPI sea NULL. No excluyas
  grupos pequeños por iniciativa propia; muestra su volumen si el usuario pide detalle.
- "Peor rendimiento", "peor disponibilidad", "peor calidad" y "peor OEE" se ordenan de menor
  a mayor. "Mejor" se ordena de mayor a menor.
- Para pérdidas económicas, horas inactivas, piezas desviadas y duración de paradas, "peor" o
  "mayor pérdida" significa ordenar de mayor a menor.
- Aplica LIMIT solamente después de agrupar, calcular el indicador y ordenar.
- Si el usuario indica solo un código numérico de artículo, como "220", "241" o "250",
  interprétalo como prefijo: usa `STARTS_WITH(UPPER(TRIM(articulo)), '220')` y devuelve
  todos los artículos que comiencen por ese código, no una coincidencia exacta.
- Para "segunda peor" o "segunda mejor", genera primero el ranking completo y selecciona
  exactamente la posición 2 mediante ROW_NUMBER o mediante ORDER BY con LIMIT 1 OFFSET 1.
  Una única fila resultante en este caso es una respuesta completa, no un resultado parcial.
- En continuaciones como "el segundo peor", conserva exactamente el periodo, universo de máquinas
  y fórmulas de la comparación anterior. Calcula producción, KPI y pérdidas una sola vez dentro de
  la misma CTE agrupada; después ordena esa CTE y selecciona la posición. Nunca vuelvas a unir el
  ranking con la vista base ni recalcules una pérdida después de aplicar LIMIT/OFFSET.
- Si el usuario solo pregunta qué entidad ocupa una posición de un ranking, selecciona únicamente
  la entidad, el KPI que determina el orden y los campos de contexto imprescindibles. No añadas
  pérdidas, producción u otros KPI que no haya solicitado en esa continuación.
- Para diferencias expresadas en puntos porcentuales, resta las proporciones y multiplica el
  resultado por 100 en SQL. Usa un alias terminado en `_puntos_porcentuales`. Por ejemplo,
  0.6518 - 0.3022 equivale a 34.96 puntos porcentuales, no a 0.35 puntos.
- En rankings de mejor o peor OEE, calcula primero calidad_pct, disponibilidad_pct y
  rendimiento_pct a partir de los SUM de cada máquina, multiplícalos, excluye NULL, ordena el
  oee_pct calculado y devuelve la posición solicitada.
- Si una consulta exterior selecciona `oee_pct`, la subconsulta o CTE interior debe crear
  explícitamente `calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct`. No selecciones
  nunca un alias que no exista en el nivel inmediatamente anterior.
- Si se pide una semana como YYYY-WW, conserva el filtro exacto `semana = 'YYYY-WW'` dentro de
  cada cálculo y en cualquier comparación posterior.
- "En lo que llevamos de año" significa desde DATE_TRUNC(CURRENT_DATE('Europe/Madrid'), YEAR)
  hasta CURRENT_DATE('Europe/Madrid'), ambos inclusive, usando `fecha` en vw_oee_master.
- En pérdidas económicas globales aplica la fórmula corporativa una sola vez a los SUM del
  conjunto completo. No sumes porcentajes ni pérdidas recalculadas por subgrupos.
- Cuando el usuario combine "alto/mayor volumen" con "peor OEE" y no indique un umbral:
  a) agrega primero por artículo para obtener producción total e indicadores del artículo;
  b) considera alto volumen a los 5 artículos con mayor total_piezas_producidas del periodo;
  c) dentro de esos 5, devuelve el artículo con menor oee_pct agregado.
  No elijas una combinación artículo-máquina residual de una sola pieza. Solo desglosa por máquina
  si el usuario lo solicita expresamente, mostrando también el volumen de cada combinación.
- Cuando el volumen de producción forme parte del criterio de selección —por ejemplo, «alto
  volumen» y «mayor beneficio si mejora el OEE»— devuelve obligatoriamente para cada artículo
  `total_piezas_producidas`, con ese alias exacto, además de `perdida_oee_eur` y `oee_pct` cuando
  proceda. El volumen no es un campo auxiliar: debe aparecer en la respuesta para justificar la
  selección.
- Si el usuario dice "que sean de volumen alto" después de recibir una combinación de volumen
  insignificante, conserva el periodo activo y aplica la definición anterior de top 5 artículos.
- Cuando el usuario pida la combinación artículo-máquina con peor/mejor KPI y establezca un mínimo
  de piezas (por ejemplo, más de 20.000), descarta cualquier shortlist o top anterior salvo que diga
  expresamente "entre esos artículos". Recalcula desde TODAS las filas del periodo activo:
  1) GROUP BY articulo, maquina;
  2) calcula total_piezas_producidas y los KPI con SUM;
  3) aplica HAVING total_piezas_producidas > mínimo;
  4) excluye KPI NULL;
  5) ORDER BY el KPI ASC para peor o DESC para mejor;
  6) LIMIT solo al final.
- Un nuevo umbral numérico indicado por el usuario sustituye cualquier criterio de "alto volumen"
  inferido previamente. No limites primero a los artículos más producidos si la pregunta solicita
  el peor OEE entre todas las combinaciones que superen el umbral.

Patrón obligatorio para peor rendimiento por máquina:
1. Filtrar primero el periodo solicitado.
2. GROUP BY maquina.
3. rendimiento_pct = LEAST(SAFE_DIVIDE(SUM(buenas + IFNULL(reoperar, 0)), SUM(piezas_teo)), 1).
4. HAVING SUM(piezas_teo) > 0 AND maquina IS NOT NULL.
5. ORDER BY rendimiento_pct ASC y después LIMIT.

Patrón obligatorio para "top N piezas/artículos más fabricados y su OEE por robot/máquina":
1. "Piezas fabricadas" o "cantidad fabricada" significa
   SUM(buenas) + SUM(IFNULL(reoperar, 0)); usa el alias total_piezas_producidas.
2. Crea una CTE que agrupe por articulo, calcule total_piezas_producidas, ordene DESC y aplique
   LIMIT N. Ese ranking es global para el periodo solicitado, no un top N diferente por máquina.
3. Une esos artículos con vw_oee_master y agrupa después por articulo y maquina.
4. En cada combinación articulo-maquina devuelve obligatoriamente articulo, maquina,
   total_piezas_producidas_maquina, calidad_pct, disponibilidad_pct, rendimiento_pct y oee_pct.
5. Conserva también el total global del artículo usado para el ranking mediante un alias como
   total_piezas_articulo, para que la respuesta pueda justificar el orden del top.
6. Calcula cada componente y el OEE con las fórmulas corporativas sobre los SUM de cada combinación;
   nunca uses AVG de OEE ni repartas el OEE global del artículo entre máquinas.
7. Ordena primero por total_piezas_articulo DESC y después por articulo y maquina.
8. Si el usuario establece un mínimo por combinación artículo-máquina, el orden obligatorio es:
   a) crear una CTE agregada por articulo y maquina con total_piezas_producidas_maquina y los SUM
      necesarios para los KPI;
   b) aplicar el mínimo mediante HAVING sobre total_piezas_producidas_maquina;
   c) sumar únicamente esas combinaciones elegibles para recalcular total_piezas_articulo;
   d) ordenar esos nuevos totales y seleccionar el top N;
   e) mostrar solamente las combinaciones elegibles.
   No calcules el total del artículo antes de aplicar el mínimo. El total mostrado debe ser siempre
   la suma exacta de los desgloses visibles. Ejemplo: si un artículo tenía 1,888,348 piezas y se
   excluye una combinación de 1 pieza, el nuevo total debe ser 1,888,347.

Patrón obligatorio para "top N paradas/incidencias más largas":
1. Devuelve como máximo una fila por `id_parada`. Una incidencia puede estar dividida en varios
   tramos con el mismo identificador: consolídalos mediante GROUP BY id_parada y
   SUM(tiempo_parada_min) antes de ordenar y limitar. No conserves únicamente el tramo mayor.
2. Incluye siempre `id_parada`, maquina, fecha_operativa, inicio_real, fin_real,
   tipo_incidencia y tiempo_parada_min para que dos incidencias parecidas puedan distinguirse.
3. Aplica el filtro SI/NO solicitado antes de ORDER BY tiempo_parada_min DESC y LIMIT N.
"""

_schema_cache: tuple[float, str, set[str]] | None = None
SCHEMA_CACHE_SECONDS = int(os.getenv("SCHEMA_CACHE_SECONDS", "600"))
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")
MEMORY_COLLECTION = os.getenv("MEMORY_COLLECTION", "chatbot_aad_conversations")
MAX_MEMORY_TURNS = int(os.getenv("MAX_MEMORY_TURNS", "8"))
APP_VERSION = "v83"
ENABLE_PROGRESS_MESSAGE = os.getenv("ENABLE_PROGRESS_MESSAGE", "true").lower() in {
    "1", "true", "yes", "si", "sí"
}
MADRID_TZ = ZoneInfo("Europe/Madrid")

SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
SPANISH_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5,
    "domingo": 6,
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

credentials, _ = google.auth.default(
    scopes=[
        "https://www.googleapis.com/auth/cloud-platform",
    ]
)
chat_credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/chat.bot"]
)
bq = bigquery.Client(project=PROJECT_ID, credentials=credentials)
memory_db = firestore.Client(
    project=PROJECT_ID,
    credentials=credentials,
    database=FIRESTORE_DATABASE,
)
ai = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
    credentials=credentials,
)

FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL|EXPORT|LOAD)\b",
    re.IGNORECASE,
)


def google_chat_text(text: str) -> str:
    """Convert common Markdown emitted by Gemini to Google Chat formatting."""
    # Google Chat uses single asterisks for bold; CommonMark's double asterisks
    # otherwise appear literally in the conversation.
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def chat_response(text: str, workspace_addon: bool):
    """Return the response envelope required by each Google Chat framework."""
    text = google_chat_text(text)
    if workspace_addon:
        return jsonify(hostAppDataAction={
            "chatDataAction": {
                "createMessageAction": {"message": {"text": text}}
            }
        })
    return jsonify(text=text)


def create_progress_message(space_name: str) -> str | None:
    """Post a temporary status message and return its Chat resource name."""
    if (
        not ENABLE_PROGRESS_MESSAGE
        or not re.fullmatch(r"spaces/[A-Za-z0-9_-]+", space_name or "")
        or space_name.startswith("spaces/eval-")
    ):
        return None
    session = AuthorizedSession(chat_credentials)
    try:
        response = session.post(
            f"https://chat.googleapis.com/v1/{space_name}/messages",
            json={"text": "Consultando datos… ⏳"},
            timeout=8,
        )
        response.raise_for_status()
        return response.json().get("name")
    except Exception:
        logging.exception("No se pudo crear el mensaje provisional de Google Chat")
        return None
    finally:
        session.close()


def update_progress_message(message_name: str, text: str) -> bool:
    """Replace a temporary Chat message with the final answer."""
    if not message_name:
        return False
    text = google_chat_text(text)
    session = AuthorizedSession(chat_credentials)
    try:
        response = session.patch(
            f"https://chat.googleapis.com/v1/{message_name}",
            params={"updateMask": "text"},
            json={"name": message_name, "text": text},
            timeout=8,
        )
        response.raise_for_status()
        return True
    except Exception:
        logging.exception("No se pudo actualizar el mensaje provisional de Google Chat")
        return False
    finally:
        session.close()


def final_chat_response(text: str, workspace_addon: bool, progress_message: str | None = None):
    """Update the status message, falling back to the normal synchronous reply."""
    if workspace_addon and progress_message and update_progress_message(progress_message, text):
        # The final message was already delivered through the Chat API.
        return jsonify({})
    return chat_response(text, workspace_addon)


def conversation_id(message: dict) -> str:
    """Build a stable, Firestore-safe identifier for a Chat conversation."""
    space = (message.get("space") or {}).get("name", "")
    sender = (message.get("sender") or {}).get("name", "")
    # A direct-message event can expose a different thread name for each message.
    # Space + sender remains stable and also keeps users isolated in group spaces.
    identity = "|".join(part for part in (space, sender) if part) or "unknown"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def load_history(conversation: str) -> list[dict[str, str]]:
    try:
        snapshot = memory_db.collection(MEMORY_COLLECTION).document(conversation).get()
        if not snapshot.exists:
            return []
        turns = snapshot.to_dict().get("turns", [])
        return [
            {"user": str(turn.get("user", "")), "assistant": str(turn.get("assistant", ""))}
            for turn in turns[-MAX_MEMORY_TURNS:]
            if isinstance(turn, dict)
        ]
    except Exception:
        logging.exception("No se pudo leer la memoria conversacional")
        return []


def save_turn(conversation: str, history: list[dict[str, str]], question: str, answer: str) -> None:
    turns = (history + [{"user": question, "assistant": answer}])[-MAX_MEMORY_TURNS:]
    try:
        memory_db.collection(MEMORY_COLLECTION).document(conversation).set(
            {"turns": turns, "updated_at": firestore.SERVER_TIMESTAMP}
        )
    except Exception:
        logging.exception("No se pudo guardar la memoria conversacional")


def clear_history(conversation: str) -> bool:
    try:
        memory_db.collection(MEMORY_COLLECTION).document(conversation).delete()
        return True
    except Exception:
        logging.exception("No se pudo borrar la memoria conversacional")
        return False


def resolve_temporal_context(text: str, today: date | None = None) -> dict[str, str]:
    """Resolve common Spanish dates into an explicit, deterministic interval."""
    current = today or datetime.now(MADRID_TZ).date()
    normalized = text.lower().strip()
    result: dict[str, str] = {}

    date_range = re.search(
        r"\b(?:(?:del|entre\s+el)\s+)?(\d{1,2})\s+(?:al\s+|y\s+(?:el\s+)?)"
        r"(\d{1,2})\s+(?:de\s+)?"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
        r"octubre|noviembre|diciembre)(?:\s+(?:de|del)?\s*(20\d{2}))?\b",
        normalized,
    )
    if date_range:
        start_day, end_day = int(date_range.group(1)), int(date_range.group(2))
        month = SPANISH_MONTHS[date_range.group(3)]
        year = int(date_range.group(4) or current.year)
        try:
            start, end = date(year, month, start_day), date(year, month, end_day)
        except ValueError:
            result["error_fecha"] = "El intervalo indicado no es válido."
            return result
        if end < start:
            result["error_fecha"] = "El intervalo indicado no es válido."
            return result
        result.update(fecha_desde=start.isoformat(), fecha_hasta=end.isoformat())
        if start > current:
            result["fecha_futura"] = "true"
        return result

    iso_range = re.search(
        r"\b(20\d{2}-\d{2}-\d{2})\s+(?:a|al|hasta|y)\s+"
        r"(20\d{2}-\d{2}-\d{2})\b",
        normalized,
    )
    if iso_range:
        try:
            start = date.fromisoformat(iso_range.group(1))
            end = date.fromisoformat(iso_range.group(2))
        except ValueError:
            result["error_fecha"] = "El intervalo indicado no es válido."
            return result
        if end < start:
            result["error_fecha"] = "El intervalo indicado no es válido."
            return result
        result.update(fecha_desde=start.isoformat(), fecha_hasta=end.isoformat())
        if start > current:
            result["fecha_futura"] = "true"
        return result

    explicit = re.search(
        r"\b(\d{1,2})(?:\s+de)?\s+"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
        r"(?:\s+(?:de|del)?\s*(20\d{2}))?\b",
        normalized,
    )
    if explicit:
        day = int(explicit.group(1))
        month = SPANISH_MONTHS[explicit.group(2)]
        year = int(explicit.group(3) or current.year)
        try:
            selected = date(year, month, day)
        except ValueError:
            result["error_fecha"] = "La fecha indicada no es válida."
            return result
        result.update(fecha_desde=selected.isoformat(), fecha_hasta=selected.isoformat())
        if selected > current:
            result["fecha_futura"] = "true"
        return result

    iso_date = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", normalized)
    if iso_date:
        try:
            selected = date(
                int(iso_date.group(1)),
                int(iso_date.group(2)),
                int(iso_date.group(3)),
            )
        except ValueError:
            result["error_fecha"] = "La fecha indicada no es válida."
            return result
        result.update(fecha_desde=selected.isoformat(), fecha_hasta=selected.isoformat())
        if selected > current:
            result["fecha_futura"] = "true"
        return result

    numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", normalized)
    if numeric:
        try:
            selected = date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))
        except ValueError:
            result["error_fecha"] = "La fecha indicada no es válida."
            return result
        result.update(fecha_desde=selected.isoformat(), fecha_hasta=selected.isoformat())
        if selected > current:
            result["fecha_futura"] = "true"
        return result

    if re.search(r"\banteayer\b", normalized):
        selected = current - timedelta(days=2)
    elif re.search(r"\bayer\b", normalized):
        selected = current - timedelta(days=1)
    elif re.search(r"\bhoy\b", normalized):
        selected = current
    else:
        selected = None
    if selected:
        return {"fecha_desde": selected.isoformat(), "fecha_hasta": selected.isoformat()}

    if re.search(r"\b(?:semana\s+(?:pasada|anterior|previa)|(?:pasada|anterior)\s+semana)\b", normalized):
        this_monday = current - timedelta(days=current.weekday())
        start = this_monday - timedelta(days=7)
        return {"fecha_desde": start.isoformat(), "fecha_hasta": (start + timedelta(days=6)).isoformat()}
    if re.search(r"\besta\s+semana\b|\bsemana\s+actual\b", normalized):
        start = current - timedelta(days=current.weekday())
        return {"fecha_desde": start.isoformat(), "fecha_hasta": current.isoformat()}
    if re.search(
        r"\b(?:la\s+)?semana\s+(?:que\s+(?:viene|entra)|siguiente|proxima|pr[oó]xima)\b|"
        r"\b(?:pr[oó]xima|siguiente)\s+semana\b",
        normalized,
    ):
        this_monday = current - timedelta(days=current.weekday())
        start = this_monday + timedelta(days=7)
        return {
            "fecha_desde": start.isoformat(),
            "fecha_hasta": (start + timedelta(days=6)).isoformat(),
        }

    # «Último trimestre» means the previous closed calendar quarter. Keeping the
    # interval explicit prevents Gemini from silently treating it as all history.
    if re.search(
        r"\b(?:ultimo|último|pasado|anterior)\s+trimestre\b|\btrimestre\s+(?:pasado|anterior)\b",
        normalized,
    ):
        current_quarter_month = ((current.month - 1) // 3) * 3 + 1
        current_quarter_start = date(current.year, current_quarter_month, 1)
        previous_quarter_end = current_quarter_start - timedelta(days=1)
        previous_quarter_month = ((previous_quarter_end.month - 1) // 3) * 3 + 1
        previous_quarter_start = date(previous_quarter_end.year, previous_quarter_month, 1)
        return {
            "fecha_desde": previous_quarter_start.isoformat(),
            "fecha_hasta": previous_quarter_end.isoformat(),
        }

    if re.search(r"\b(?:este|actual)\s+trimestre\b", normalized):
        current_quarter_month = ((current.month - 1) // 3) * 3 + 1
        current_quarter_start = date(current.year, current_quarter_month, 1)
        return {
            "fecha_desde": current_quarter_start.isoformat(),
            "fecha_hasta": current.isoformat(),
        }

    # Explicit calendar months, e.g. «agosto», «mes de agosto» or
    # «agosto de 2026», establish a complete month interval and replace any
    # narrower date inherited from the previous turn.
    month_match = re.search(
        r"\b(?:mes\s+de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
        r"septiembre|setiembre|octubre|noviembre|diciembre)"
        r"(?:\s+(?:de|del)\s*(20\d{2}))?\b",
        normalized,
    )
    if month_match:
        month = SPANISH_MONTHS[month_match.group(1)]
        year = int(month_match.group(2) or current.year)
        start = date(year, month, 1)
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        end = next_month - timedelta(days=1)
        result.update(fecha_desde=start.isoformat(), fecha_hasta=end.isoformat())
        if start > current:
            result["fecha_futura"] = "true"
        return result

    weekday_match = re.search(
        r"\b(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)\s+pasad[oa]\b",
        normalized,
    )
    if weekday_match:
        target = SPANISH_WEEKDAYS[weekday_match.group(1)]
        delta = (current.weekday() - target) % 7 or 7
        selected = current - timedelta(days=delta)
        return {"fecha_desde": selected.isoformat(), "fecha_hasta": selected.isoformat()}
    return result


def normalized_business_text(text: str) -> str:
    """Normalize accents and punctuation so common plant terminology is recognized."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9._/-]+", " ", folded).strip()


def lexical_intent_text(text: str) -> str:
    """Return normalized text plus canonical intent terms for natural plant vocabulary."""
    normalized = normalized_business_text(text)
    aliases = {
        r"\b(?:eficiencia\s+global|eficiencia\s+de\s+equipos?|efectividad)\b": "oee",
        r"\b(?:disponible|tiempo\s+disponible|disponibilidad\s+operativa)\b": "disponibilidad",
        r"\b(?:velocidad|productividad|ritmo|performance|eficiencia\s+de\s+produccion)\b": "rendimiento",
        r"\b(?:rechazadas?|defectuosas?|malas?|retrabajo|retrabajadas?)\b": "reoperar calidad",
        r"\b(?:paro|paros|detenido|detenida|downtime|tiempo\s+perdido)\b": "parada",
        r"\b(?:orden(?:es)?\s+de\s+fabricacion|of|ofs|pev|pevs|pedido(?:s)?)\b": "ordenes pendientes",
        r"\b(?:referencia|ref\.?|sku|codigo\s+de\s+pieza)\b": "articulo",
        r"\b(?:planta|linea|celula|puesto|recurso)\b": "maquina",
        r"\b(?:carga\s+de\s+trabajo|workload|cola|cartera|backlog)\b": "carga pendiente",
        r"\b(?:tendencia|evolucion|serie\s+temporal|mes\s+a\s+mes)\b": "evolucion",
        r"\b(?:sube|subida|crece|crecimiento|mejora|mejorado|mejorando)\b": "mejora",
        r"\b(?:baja|bajada|cae|caida|empeora|empeorado|empeorando)\b": "empeora",
    }
    canonical = normalized
    for pattern, replacement in aliases.items():
        if re.search(pattern, normalized):
            canonical += " " + replacement
    return canonical


def extract_oee_target_pct(text: str) -> float | None:
    """Extract an explicit OEE target such as 'subir el OEE al 65 %'."""
    normalized = normalized_business_text(text)
    patterns = (
        r"\b(?:oee|ooe)\b.{0,45}?\b(?:al|hasta|objetivo(?:\s+de)?|meta(?:\s+de)?)\s*(\d{1,3}(?:[.,]\d+)?)\s*%?",
        r"\b(?:subir|subimos|elevar|elevamos|mejorar|mejoramos|llevar|llevamos|alcanzar)\b"
        r".{0,45}?(\d{1,3}(?:[.,]\d+)?)\s*%?.{0,20}?\b(?:oee|ooe)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        value = float(match.group(1).replace(",", "."))
        if 0 < value <= 100:
            return value
    return None


def is_oee_target_simulation(question: str) -> bool:
    """Recognize hypothetical savings/benefit questions tied to an OEE target."""
    normalized = lexical_intent_text(question)
    has_economic_intent = bool(re.search(
        r"\b(?:ahorr\w*|benefici\w*|impacto\s+economico|reducci\w*\s+(?:de\s+)?perdida|"
        r"cuanto\s+dejariamos\s+de\s+perder)\b",
        normalized,
    ))
    return has_economic_intent and extract_oee_target_pct(question) is not None


def is_high_volume_oee_opportunity(question: str) -> bool:
    """Recognize selections whose business justification depends on production volume."""
    normalized = lexical_intent_text(question)
    mentions_volume = bool(re.search(
        r"\b(?:alto|gran|mayor)\s+volumen\b|\bvolumen\s+(?:alto|muy\s+alto|elevado)\b|"
        r"\bpor\s+su\s+volumen\s+de\s+produccion\b",
        normalized,
    ))
    mentions_opportunity = bool(re.search(
        r"\b(?:beneficio|ahorro|potencial|mejor\w*|subir|elevar)\b",
        normalized,
    ))
    return mentions_volume and mentions_opportunity and bool(re.search(r"\b(?:oee|ooe)\b", normalized))


def query_mode(question: str, history: list[dict[str, str]]) -> str:
    """Describe the requested output shape so follow-up prompts keep their intent."""
    normalized = lexical_intent_text(question)
    business_text = normalized
    if re.search(
        r"\b(planificador|planificacion|planning|capacidad|carga|cargas|backlog|cartera|"
        r"pedidos? pendientes?|ordenes? pendientes?|ofs?|pevs?|cola de produccion|"
        r"trabajo pendiente|ocupacion|saturacion|sobrecarga|desviacion|desvio|"
        r"redistribu\w*|reasigna\w*|repartir|trasladar|pasar carga|mover produccion|"
        r"fecha de necesidad|fecha de entrega|fecha de compromiso|vencimiento|vence|vencen|caduca|caducan|"
        r"semana de necesidad)\b",
        business_text,
    ):
        return "planificador"
    asks_oee_component_breakdown = bool(
        re.search(r"\b(?:disponibilidad|oee|calidad|rendimiento)\b", normalized)
        and re.search(r"\b(?:desglos\w*|componentes?|descompon\w*|calculo)\b", normalized)
    )
    if asks_oee_component_breakdown:
        return "global"
    if re.search(
        r"\b(parada|paradas|incidencia|incidencias|averia|averias|tiempo muerto|"
        r"tiempos muertos|detencion|detenciones|interrupcion|interrupciones)\b",
        business_text,
    ):
        return "paradas"
    if re.search(r"\b(ranking|top|mejor|peor|segunda|segundo|tercera|tercero)\b", normalized):
        return "ranking"
    wants_global = bool(re.search(r"\b(global|total|consolidado|consolidada)\b", normalized))
    wants_detail = bool(re.search(r"\b(detalle|desglose|por\s+art[ií]culo|todo)\b", normalized))
    if wants_global and wants_detail:
        return "global_y_por_articulo"
    if wants_detail:
        return "por_articulo"
    if wants_global:
        return "global"
    # An explicit KPI request starts a new analytical intent. It must not inherit
    # a previous stop-analysis mode merely because the period is conversational.
    if re.search(
        r"\b(oee|ooe|calidad|disponibilidad|rendimiento|horas\s+inactivas|"
        r"piezas\s+desviadas|reoperar|reoperaciones?|retrabajo|tiempo\s+de\s+ciclo|"
        r"ciclo\s+(?:medio|promedio)|tasa\s+de?\s*rechazo|p[eé]rdida)\b",
        normalized,
    ):
        return "global"
    for turn in reversed(history):
        previous = query_mode(str(turn.get("user", "")), [])
        if previous != "general":
            return previous
    return "general"


def _conversation_domain(question: str) -> str | None:
    """Classify only the data domain needed to prevent cross-domain filter reuse."""
    mode = query_mode(question, [])
    normalized = lexical_intent_text(question)
    if mode == "planificador":
        return "planificador"
    if re.search(r"\b(?:balanceo|redistribu|sugerencias? de balanceo)\b", normalized):
        return "balanceo"
    if mode == "paradas":
        return "paradas"
    if re.search(
        r"\b(?:oee|ooe|calidad|disponibilidad|rendimiento|produccion|piezas?|"
        r"reoperaciones?|perdida(?:s)? economica(?:s)?)\b",
        normalized,
    ):
        return "oee"
    return None


def _reconcile_period_only_kpi_follow_up(
    question: str, rewritten: str, history: list[dict[str, str]]
) -> str:
    """Preserve stated intent, and carry a single-machine KPI through a period-only turn."""
    normalized = lexical_intent_text(question)
    rewritten_normalized = lexical_intent_text(rewritten)
    explicit_domain = _conversation_domain(question)
    if explicit_domain and _conversation_domain(rewritten) != explicit_domain:
        return question

    explicit_metric_patterns = (
        r"\b(?:oee|ooe)\b", r"\bcalidad\b", r"\bdisponibilidad\b",
        r"\b(?:rendimiento|performance)\b", r"\bproducci[oó]n\b",
    )
    for pattern in explicit_metric_patterns:
        if re.search(pattern, normalized) and not re.search(pattern, rewritten_normalized):
            return question
    explicit_machines = {
        f"RB{match.group(1)}"
        for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", question, re.I)
    }
    rewritten_machines = {
        f"RB{match.group(1)}"
        for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", rewritten, re.I)
    }
    has_explicit_article = bool(re.search(
        r"\b(?:art[ií]culo|referencia|producto|pieza)\s+[A-Z0-9][A-Z0-9._-]{5,}\b",
        question, re.I,
    ))
    machine_only_update = bool(explicit_machines) and not explicit_domain and not any(
        re.search(pattern, normalized) for pattern in explicit_metric_patterns
    ) and not has_explicit_article
    if explicit_machines - rewritten_machines and not (machine_only_update and history):
        return question

    explicit_global = bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots?|m[aá]quinas?|equipos?)\b|"
        r"\b(?:global(?:es)?|de\s+planta)\b",
        normalized,
    ))
    rewritten_global = bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots?|m[aá]quinas?|equipos?)\b|"
        r"\b(?:global(?:es)?|de\s+planta)\b",
        rewritten_normalized,
    ))
    if explicit_global and not rewritten_global:
        return question

    if not history:
        explicit_metrics = any(re.search(pattern, normalized) for pattern in explicit_metric_patterns)
        rewritten_metrics = any(
            re.search(pattern, rewritten_normalized) for pattern in explicit_metric_patterns
        )
        if rewritten_metrics and not explicit_metrics:
            return question
        if rewritten_machines - explicit_machines:
            return question

    has_period = bool(
        re.search(r"\b20\d{2}\s*[-/]\s*\d{1,2}\b|\b(?:semana|s)\s*[-_ ]?\s*\d{1,2}\b", question, re.I)
        or resolve_temporal_context(question).get("fecha_desde")
    )
    has_explicit_metric_or_domain = bool(
        re.search(
            r"\b(?:oee|ooe|calidad|disponibilidad|rendimiento|producci[oó]n|piezas?|"
            r"paradas?|aver[ií]as?|balanceo|planificador|carga|ranking|comparar|compara)\b",
            normalized,
        )
    )
    broad_scope = bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots?|m[aá]quinas?|equipos?)\b|"
        r"\b(?:global(?:es)?|de\s+planta)\b",
        normalized,
    ))
    if (
        not history or (not has_period and not machine_only_update)
        or has_explicit_metric_or_domain or has_explicit_article
        or broad_scope or _conversation_domain(question)
    ):
        return rewritten

    domains = [_conversation_domain(str(turn.get("user", ""))) for turn in history]
    last_domain = next((domain for domain in reversed(domains) if domain), None)
    if last_domain != "oee":
        return rewritten
    last_other_domain = max(
        (index for index, domain in enumerate(domains) if domain and domain != "oee"),
        default=-1,
    )
    same_domain_history = history[last_other_domain + 1:]

    metric_patterns = {
        "Disponibilidad": r"\bdisponibilidad\b",
        "Calidad": r"\bcalidad\b",
        "Rendimiento": r"\b(?:rendimiento|performance)\b",
        "OEE": r"\b(?:oee|ooe)\b",
    }
    metric = None
    comparison_context = False
    for turn in reversed(same_domain_history):
        previous_question = lexical_intent_text(str(turn.get("user", "")))
        matches = [label for label, pattern in metric_patterns.items() if re.search(pattern, previous_question)]
        if matches:
            # A multi-metric request has no single indicator to carry forward.
            if len(matches) != 1:
                return rewritten
            if re.search(r"\b(?:ranking|mejor|peor|top)\b", previous_question):
                return rewritten
            metric = matches[0]
            comparison_context = bool(re.search(
                r"\b(?:compara\w*|comparaci[oó]n|entre|versus|vs\.?|diferencia)\b",
                previous_question,
            ))
            break
    if not metric:
        return rewritten

    filters = active_filters(question, history)
    if filters.get("requiere_aclaracion") or filters.get("maquinas"):
        if not comparison_context or filters.get("requiere_aclaracion"):
            return rewritten
    machine = filters.get("maquina")
    comparison_machines = filters.get("maquinas", "").split(",")
    if comparison_context:
        if len(comparison_machines) < 2:
            return rewritten
    elif not machine:
        return rewritten

    parts = [metric]
    if comparison_context:
        parts = ["Comparar", metric, "de", " y ".join(comparison_machines)]
    else:
        parts.extend(["de la máquina", machine])
    if filters.get("articulo"):
        parts.extend(["del artículo", filters["articulo"]])
    if filters.get("articulo_prefijo"):
        parts.extend(["del artículo cuyo código empieza por", filters["articulo_prefijo"]])
    if filters.get("semana"):
        parts.extend(["en la semana", filters["semana"]])
    elif filters.get("fecha_desde"):
        start, end = filters["fecha_desde"], filters.get("fecha_hasta", filters["fecha_desde"])
        parts.extend(["entre", start, "y", end])
    elif filters.get("anio"):
        parts.extend(["en el año", filters["anio"]])
    else:
        return rewritten
    return " ".join(parts)


def _ambiguous_machine_choice(question: str) -> tuple[str, ...]:
    """Return alternatives when a machine choice is phrased as unresolved 'A or B'."""
    machines = tuple(dict.fromkeys(
        f"RB{match.group(1)}"
        for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", question, re.IGNORECASE)
    ))
    comparison = bool(re.search(
        r"\b(?:compara\w*|comparaci[oó]n|entre|versus|vs\.?|diferencia|puntos? separan)\b",
        normalized_business_text(question),
    ))
    if (
        len(machines) > 1
        and re.search(r"\b(?:o|u)\b", normalized_business_text(question))
        and not comparison
    ):
        return machines
    return ()


def active_filters(question: str, history: list[dict[str, str]]) -> dict[str, str]:
    """Recover stable domain filters from the current and recent user turns."""
    current_normalized = normalized_business_text(question)
    explicit_current_scope = bool(
        resolve_temporal_context(question).get("fecha_desde")
        or re.search(r"\b20\d{2}\s*[-/]\s*\d{1,2}\b", question)
        or re.search(r"\b(?:semana|s)\s*[-_/]?\s*\d{1,2}\b", question, re.IGNORECASE)
    )
    annual_scope = bool(re.search(r"\b20\d{2}\b", current_normalized)) and not bool(
        re.search(
            r"\b(?:semana|s\s*[-_/]?\s*\d{1,2}|20\d{2}\s*[-/]\s*\d{1,2}|ayer|pasado|anterior)\b",
            current_normalized,
        )
    )
    explicit_historical_current = bool(re.search(
        r"\b(?:historico|historica|historicamente|todo\s+el\s+historico|"
        r"todos\s+los\s+datos|periodo\s+completo)\b",
        current_normalized,
    ))
    current_period_explicit = explicit_current_scope or annual_scope or explicit_historical_current
    contextual_follow_up = bool(re.search(
        r"\b(?:ese|esa|eso|esos|esas|mismo|misma|anterior|antes|"
        r"comparalo|comparala|dame\s+detalles|por\s+que)\b",
        current_normalized,
    ))
    broad_standalone_request = bool(
        not contextual_follow_up
        and not re.search(r"\b(?:mejor|peor|segund|tercer)\w*\b", current_normalized)
        and re.search(
            r"\b(?:robots|equipos|maquinas|referencias|articulos|piezas)\b",
            current_normalized,
        )
        and re.search(
            r"\b(?:sobrecargad|saturad|mas\s+cantidad|mayor\s+cantidad|"
            r"carga\s+pendiente|segun\s+el\s+plan|ranking|global)\w*\b",
            current_normalized,
        )
    )
    # Keep only the active analytical domain. Temporal changes are merged by
    # dimension below instead of dropping the whole conversation scope.
    current_domain = _conversation_domain(question)
    domain_history = [
        (index, _conversation_domain(str(turn.get("user", ""))))
        for index, turn in enumerate(history)
    ]
    last_domain = next((domain for _, domain in reversed(domain_history) if domain), None)
    if current_domain and last_domain and current_domain != last_domain:
        normalized_question = lexical_intent_text(question)
        contextual_oee_stop_explanation = bool(
            current_domain == "paradas"
            and last_domain == "oee"
            and re.search(r"\b(?:por\s+que|explica\w*|causa\w*|motivo\w*)\b", normalized_question)
            and re.search(r"\b(?:disponibilidad|oee|rendimiento|calidad|resultado|baja|bajo)\b", normalized_question)
            and not explicit_historical_current
        )
        if contextual_oee_stop_explanation:
            latest_oee_index = next(
                index for index, domain in reversed(domain_history) if domain == "oee"
            )
            history = [history[latest_oee_index]]
        else:
            history = []
    elif last_domain:
        last_other_domain = max(
            (index for index, domain in domain_history if domain and domain != last_domain),
            default=-1,
        )
        history = history[last_other_domain + 1:]

    explicit_global = bool(re.search(
        r"\b(?:global(?:es)?|de\s+planta|todos\s+los\s+datos)\b",
        current_normalized,
    ))
    clears_machine = explicit_global or bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots?|m[aá]quinas?|equipos?)\b",
        current_normalized,
    ))
    clears_article = explicit_global or bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:art[ií]culos?|referencias?|piezas?)\b",
        current_normalized,
    ))
    if broad_standalone_request:
        clears_machine = clears_machine or bool(re.search(
            r"\b(?:robots?|equipos?|m[aá]quinas?)\b", current_normalized
        ))
        clears_article = clears_article or bool(re.search(
            r"\b(?:referencias?|art[ií]culos?|piezas?)\b", current_normalized
        ))
    # Include assistant summaries when resolving elliptical follow-ups such as
    # “la pérdida del segundo”; the preceding ranked robot is often present
    # only in the assistant's previous answer.
    user_texts: list[tuple[str, bool, bool]] = []
    for turn in history:
        user_texts.append((str(turn.get("user", "")), False, False))
        user_texts.append((str(turn.get("assistant", "")), True, False))
    user_texts.append((question, False, True))
    result: dict[str, str] = {"modo_consulta": query_mode(question, history)}
    ambiguity = _ambiguous_machine_choice(question)
    if not ambiguity and current_period_explicit:
        for turn in reversed(history):
            prior_question = str(turn.get("user", ""))
            ambiguity = _ambiguous_machine_choice(prior_question)
            if ambiguity:
                break
            if re.search(r"\bRB\s*[-_ ]?\s*\d+\b", prior_question, re.IGNORECASE):
                break
    if ambiguity:
        result["requiere_aclaracion"] = "maquinas"
        result["alternativas_maquina"] = ",".join(ambiguity)
    prefix_match = re.search(
        r"\b(?:empiez\w*|comienz\w*|prefijo|c[oó]digo)\D{0,25}(\d{3,6})\b",
        question,
        re.IGNORECASE,
    )
    if prefix_match:
        result["articulo_prefijo"] = prefix_match.group(1)
    for text, is_assistant, is_current in reversed(user_texts):
        if "alcance_temporal" not in result and re.search(
            r"\b(?:historico|historica|historicamente|todo\s+el\s+historico|"
            r"todos\s+los\s+datos|periodo\s+completo)\b",
            normalized_business_text(text),
        ):
            result["alcance_temporal"] = "historico"
        prior_assistant_entity_unsafe = is_assistant and current_period_explicit
        if (
            not is_assistant
            and (is_current or not clears_machine)
            and "maquina" not in result
            and "maquinas" not in result
        ):
            named_machines = tuple(dict.fromkeys(
                f"RB{match.group(1)}"
                for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", text, re.IGNORECASE)
            ))
            if len(named_machines) > 1:
                result["maquinas"] = ",".join(named_machines)
        if (
            "maquina" not in result
            and "maquinas" not in result
            and not prior_assistant_entity_unsafe
            and (is_current or not clears_machine)
        ):
            machine = re.search(r"\bRB\s*[-_ ]?\s*(\d+)\b", text, re.IGNORECASE)
            if not machine and not (is_current and clears_machine):
                machine = re.search(
                    r"\b(?:robot|m[aá]quina|equipo)\s*(?:RB\s*)?[-_ ]?\s*(\d+)\b",
                    text,
                    re.IGNORECASE,
                )
            if machine:
                result["maquina"] = f"RB{machine.group(1)}"
        if (
            "articulo" not in result
            and "articulo_prefijo" not in result
            and (is_current or not clears_article)
            and not prior_assistant_entity_unsafe
        ):
            article = re.search(
                r"\b(?:art[ií]culo|pieza|referencia|producto)\s+([A-Z0-9][A-Z0-9.\-]{7,})\b",
                text,
                re.IGNORECASE,
            )
            if not article:
                article = re.search(
                    r"\b(?=[A-Z0-9.\-]*[A-Z])(?=[A-Z0-9.\-]*\d)([A-Z0-9][A-Z0-9.\-]{7,})\b",
                    text,
                    re.IGNORECASE,
                )
            if article and not re.fullmatch(r"RB\d+", article.group(1), re.IGNORECASE):
                result["articulo"] = article.group(1).upper()
        if (is_current or not current_period_explicit) and "semana" not in result and "fecha_desde" not in result:
            week = re.search(r"\b(20\d{2})\s*[-/]\s*(\d{1,2})\b(?!\s*[-/])", text)
            if week:
                result["semana"] = f"{week.group(1)}-{int(week.group(2)):02d}"
            else:
                short_week = re.search(
                    r"\b(?:semana\s*|s\s*)(\d{1,2})\b",
                    text,
                    re.IGNORECASE,
                )
                if short_week and 1 <= int(short_week.group(1)) <= 53:
                    explicit_year = re.search(r"\b(20\d{2})\b", text)
                    year = int(explicit_year.group(1)) if explicit_year else datetime.now(MADRID_TZ).year
                    result["semana"] = f"{year}-{int(short_week.group(1)):02d}"
        if (is_current or not current_period_explicit) and "anio" not in result:
            year = re.search(r"\b(20\d{2})\b", text)
            if year:
                result["anio"] = year.group(1)
        if (
            (is_current or not current_period_explicit)
            and "fecha_desde" not in result
            and "semana" not in result
        ):
            temporal = resolve_temporal_context(text)
            if temporal.get("fecha_desde"):
                result["fecha_desde"] = temporal["fecha_desde"]
                result["fecha_hasta"] = temporal["fecha_hasta"]
        # A genuinely broad request establishes a new scope boundary. A bare
        # follow-up such as “las cinco paradas OEE más largas” must still inherit
        # the preceding machine/week. Stop only when the message is explicitly
        # global or introduces its own temporal scope.
        text_explicit_global = bool(re.search(
            r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots|m[aá]quinas|art[ií]culos|incidencias|paradas)\b|"
            r"\b(?:global|globales|de\s+planta)\b",
            text,
            re.IGNORECASE,
        ))
        explicit_temporal_scope = bool(resolve_temporal_context(text).get("fecha_desde"))
        # A year-only request is also a temporal scope boundary.  Once a turn
        # changes from a specific week to the whole year, later follow-ups must
        # not recover that older week from deeper conversation history.
        explicit_annual_scope = bool(re.search(r"\b20\d{2}\b", text)) and not bool(
            re.search(
                r"\b(?:semana|s\s*[-_/]?\s*\d{1,2}|20\d{2}\s*[-/]\s*\d{1,2}|ayer|pasado|anterior)\b",
                normalized_business_text(text),
            )
        )
        explicit_historical_scope = bool(re.search(
            r"\b(?:historico|historica|historicamente|todo\s+el\s+historico|"
            r"todos\s+los\s+datos|periodo\s+completo)\b",
            normalized_business_text(text),
        ))
        broad_planner = bool(re.search(
            r"\b(?:cartera|backlog|carga|ocupaci[oó]n|saturaci[oó]n|sobrecarga|"
            r"desviaci[oó]n|desv[ií]o|turnos?\s+(?:previstos?|planificados?|ajustados?)|planificador)\b",
            text,
            re.IGNORECASE,
        ))
        if not is_current and not is_assistant and (
            text_explicit_global
            or explicit_temporal_scope
            or explicit_annual_scope
            or explicit_historical_scope
            or broad_planner
        ):
            break
    mentioned_machines = sorted({
        f"RB{match.group(1)}"
        for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", question, re.IGNORECASE)
    })
    if len(mentioned_machines) > 1:
        result["maquinas"] = ",".join(mentioned_machines)
        result.pop("maquina", None)
    return result


def context_question_answer(question: str, history: list[dict[str, str]]) -> str | None:
    """Answer questions about the active analytical scope without generating SQL."""
    normalized = question.lower().strip(" `¿?¡!.,")
    asks_scope = bool(re.search(
        r"(estas|estás).*(usando|utilizando|filtrando|dando).*(datos|periodo|año|semana|filtro|20\d{2})|"
        r"(estas|estás).*(datos|periodo|año|semana|filtro).*(usando|utilizando|filtrando)|"
        r"(que|qué|cual|cuál)\s+(?:es\s+)?(?:el|la)?\s*(periodo|año|semana|filtro)\s+(activo|activa|usado|usada|utilizado|utilizada)|"
        r"(que|qué|cual|cuál)\s+(periodo|año|semana|filtro).*(usando|utilizando)|"
        r"datos\s+solo\s+(?:de\s+)?20\d{2}|"
        r"solo\s+.*datos\s+(?:de\s+)?20\d{2}|"
        r"(?:qu[eé]|cu[aá]les?)\s+datos\s+(?:est[aá]s\s+)?(?:usando|utilizando)|"
        r"(?:qu[eé]|cu[aá]les?)\s+fechas?.*(?:usad|utiliz|aplic|tomad|emplead|considerad)|"
        r"(?:qu[eé]|cu[aá]les?)\s+(?:fechas?|intervalo).*(?:trimestre|periodo)|"
        r"(?:qu[eé]|cu[aá]l)\s+alcance.*(?:mantenido|conservado|usado|utilizado|aplicado)",
        normalized,
    ))
    if not asks_scope:
        return None

    if re.search(r"(?:qu[eé]|cuales?)\s+datos\s+(?:est[aá]s\s+)?(?:usando|utilizando)", normalized):
        return (
            "Utilizo las vistas autorizadas vw_oee_master para producción y OEE, "
            "vw_import_paradas para paradas e incidencias, y vw_planificador_capacidad "
            "para cartera y carga pendiente."
        )

    # Resolve temporal expressions in the scope question itself before relying
    # on compacted conversation memory.  This is important for follow-ups such
    # as «¿Qué fechas has usado para el último trimestre?»: the phrase carries
    # a complete closed-quarter range, while the stored summary may retain only
    # its start date.
    current_temporal = resolve_temporal_context(question)
    if (
        current_temporal.get("fecha_desde")
        and current_temporal.get("fecha_hasta")
        and current_temporal["fecha_desde"] != current_temporal["fecha_hasta"]
        and re.search(r"(?:fechas?|intervalo|periodo).*(?:trimestre|semana|mes|año|periodo)", normalized)
    ):
        return (
            f"Estoy utilizando el periodo del {current_temporal['fecha_desde']} "
            f"al {current_temporal['fecha_hasta']}."
        )

    previous = active_filters("", history)
    asks_maintained_scope = bool(re.search(
        r"(?:qu[eé]|cu[aá]l)\s+alcance.*(?:mantenido|conservado|usado|utilizado|aplicado)",
        normalized,
    ))
    if asks_maintained_scope:
        scope_parts = []
        if previous.get("fecha_desde"):
            if previous["fecha_desde"] == previous.get("fecha_hasta"):
                scope_parts.append(f"la fecha {previous['fecha_desde']}")
            else:
                scope_parts.append(
                    f"el periodo {previous['fecha_desde']}–{previous['fecha_hasta']}"
                )
        elif previous.get("semana"):
            scope_parts.append(f"la semana {previous['semana']}")
        elif previous.get("anio"):
            scope_parts.append(f"el año {previous['anio']}")
        elif previous.get("alcance_temporal") == "historico":
            scope_parts.append("todo el histórico disponible")
        else:
            scope_parts.append("todos los datos disponibles")
        if previous.get("articulo"):
            scope_parts.append(f"el artículo {previous['articulo']}")
        if previous.get("maquina"):
            scope_parts.append(f"la máquina {previous['maquina']}")
        return "He mantenido " + ", ".join(scope_parts) + ", sin introducir filtros nuevos."
    mentioned_year = re.search(r"\b(20\d{2})\b", normalized)
    asks_year_scope = bool(re.search(r"\b(?:solo|solamente|únicamente)\b.*\bdatos\b|\bdatos\b.*\b(?:solo|solamente|únicamente)\b", normalized))
    if asks_year_scope and not previous.get("anio"):
        return "Sí. El periodo de trabajo actual corresponde a los datos disponibles de 2026."
    if mentioned_year and asks_year_scope and previous.get("anio"):
        year = previous["anio"]
        if mentioned_year.group(1) == year:
            return f"Sí. Estoy utilizando únicamente los datos de {year} indicados en la consulta anterior."
        return f"No. El periodo activo procede de la consulta anterior y corresponde a {year}."
    if previous.get("fecha_desde"):
        if previous["fecha_desde"] == previous.get("fecha_hasta"):
            return f"Estoy utilizando el {previous['fecha_desde']} como fecha activa."
        return (
            f"Estoy utilizando el periodo del {previous['fecha_desde']} "
            f"al {previous['fecha_hasta']}."
        )
    if previous.get("semana"):
        return f"Estoy utilizando la semana {previous['semana']} como periodo activo."
    if previous.get("anio"):
        year = previous["anio"]
        if mentioned_year:
            if mentioned_year.group(1) == year:
                return f"Sí. Estoy utilizando únicamente los datos de {year} indicados en la consulta anterior."
            return f"No. El periodo activo procede de la consulta anterior y corresponde a {year}."
        return f"El periodo activo corresponde a {year}."
    return "La consulta anterior no tenía un periodo explícito; se utilizaron todos los datos disponibles."


def capability_response(question: str, history: list[dict[str, str]]) -> str | None:
    """Handle conversational scope questions without invoking SQL or Gemini."""
    normalized = lexical_intent_text(question)
    asks_identity = bool(re.search(
        r"\b(?:c[oó]mo te llamas|qui[eé]n eres|para qu[eé] sirves|qu[eé] haces)\b",
        normalized,
    ))
    asks_process = bool(re.search(
        r"\b(?:c[oó]mo trabajas|c[oó]mo funciona(?:s)?|c[oó]mo calculas|de d[oó]nde salen|"
        r"qu[eé] vistas|qu[eé] fuentes|qu[eé] proceso|qu[eé] procesos|sincronizaci[oó]n|"
        r"actualiz[aá]is|actualizas|construyes la respuesta)\b",
        normalized,
    ))
    asks_capabilities = bool(re.search(
        r"\b(?:alcance|capacidades|funciones)\b|"
        r"(?:qu[eé]|cuales|cu[aá]les).{0,35}(?:tipo de preguntas|preguntas).{0,25}(?:puedo|puedes|hacer|realizar)|"
        r"\b(?:qu[eé]\s+puedes\s+hacer|en\s+qu[eé]\s+puedes\s+ayudar(?:me)?|"
        r"qu[eé]\s+ayuda\s+puedes\s+dar(?:me)?)\b",
        normalized,
    ))
    if not asks_capabilities and not asks_process:
        if not asks_identity:
            return None
    if asks_identity:
        return (
            "Soy el Asistente OEE - AAD. Sirvo para consultar y analizar datos de "
            "producción, OEE y paradas de planta desde Google Chat. Puedo ayudarte "
            "con indicadores, comparaciones, rankings, pérdidas económicas y causas "
            "de parada por máquina, artículo, turno o semana."
        )
    if asks_process:
        return (
            "Trabajo en dos capas. Cuando preguntas por cifras, consulto las vistas "
            "autorizadas de BigQuery: `vw_oee_master` para producción e indicadores "
            "OEE, `vw_import_paradas` para incidencias y tiempos de parada, y "
            "`vw_planificador_capacidad` para cartera, carga pendiente, fechas de "
            "necesidad y desviaciones del plan, y `vw_sugerencias_balanceo` para las "
            "propuestas de redistribución ya calculadas.\n\n"
            "Después valido que la consulta sea solo de lectura, aplico los filtros "
            "de máquina, artículo, turno, fecha o semana y calculo los indicadores "
            "con las fórmulas corporativas. Gemini me ayuda a interpretar la "
            "pregunta y redactar el resultado, pero no es la fuente de los datos.\n\n"
            "También puedo explicar el proceso, las fórmulas y el alcance del "
            "asistente sin consultar BigQuery. La memoria solo conserva el contexto "
            "de la conversación; no almacena ni modifica la producción. No consulto "
            "Google Drive. Si una vista no tiene datos o la sincronización está "
            "pausada, debo indicarlo y no inventar una respuesta."
        )
    return (
        "Puedo ayudarte a analizar el OEE y las paradas de planta. Puedo consultar:\n\n"
        "• OEE, Calidad, Disponibilidad y Rendimiento por máquina, artículo, turno o semana.\n"
        "• Comparaciones y rankings: mejor o peor máquina, segunda peor, mejores artículos y robots.\n"
        "• Producción, piezas teóricas, reoperaciones y pérdidas económicas.\n"
        "• Número, duración, causas y clasificación de paradas OEE/no planificadas y No OEE/planificadas.\n"
        "• Carga pendiente, horas y turnos planificados, desviaciones y necesidades futuras por robot.\n"
        "• Sugerencias calculadas de balanceo: origen, destino, piezas a mover y turnos liberados.\n"
        "• Fechas relativas como «ayer», «viernes pasado» o «semana anterior».\n\n"
        "Indica siempre que puedas el periodo, la máquina o el artículo. También puedes "
        "escribir «Reiniciar memoria» para comenzar una conversación sin el contexto anterior."
    )


def balance_application_status_response(question: str) -> str | None:
    """Explain that calculated balance suggestions never mutate the production plan."""
    normalized = lexical_intent_text(question)
    mentions_balance = bool(re.search(
        r"\b(?:sugerenc|propuest|balance|redistribu|reasigna|movimiento)\w*\b",
        normalized,
    ))
    asks_if_applied = bool(re.search(
        r"\b(?:aplicad|ejecutad|realizad|modificad|cambiad|actualizad)\w*\b|"
        r"\b(?:se\s+han|ya\s+se|automaticamente)\b",
        normalized,
    ))
    if not (mentions_balance and asks_if_applied):
        return None
    return (
        "No. Las sugerencias de balanceo son recomendaciones informativas y de solo lectura. "
        "No se aplican automáticamente ni modifican el plan; cualquier movimiento debe ser "
        "validado y ejecutado por el equipo responsable de planificación."
    )


def is_explanation_request(question: str) -> bool:
    normalized = lexical_intent_text(question)
    return bool(re.search(
        r"^(pero\s+)?(explica|explícamelo|explicamelo|explícame|explicame|aclárame|aclarame)(lo|me)?\b|"
        r"^((pero|y)\s+)?(por\s+que|por\s+qué)\s+(no|has|dijiste|elegiste|sale)|"
        r"no\s+entiendo\s+(la|el|por)|"
        r"\b(?:por\s+que|por\s+qué|raz[oó]n|motivo)\b.*\b(?:dices|sale|resultado|respuesta|eso)\b",
        normalized,
    ))


def explain_history(question: str, history: list[dict[str, str]]) -> str:
    prompt = f"""
Eres el asistente interno de OEE. El usuario pide explicar o revisar una respuesta anterior.
Responde en español basándote exclusivamente en el historial adjunto, sin generar SQL ni afirmar
que faltan permisos.

Reglas:
- Compara literalmente las cifras relevantes del historial.
- Si dos respuestas se contradicen o una selección anterior no cumple el criterio pedido,
  reconócelo claramente como un error y explica la causa lógica probable.
- Un OEE menor es peor. Un valor de producción supera un mínimo solo si cumple el umbral indicado.
- No defiendas una respuesta incorrecta ni inventes información ausente.
- Termina indicando cuál debería ser la conclusión correcta con los datos disponibles.
- Sé conciso: máximo 5 frases.

Historial: {json.dumps(history, ensure_ascii=False)}
Petición actual: {question}
"""
    response = ai.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0,
            thinking_config=ThinkingConfig(thinking_budget=0),
            max_output_tokens=512,
        ),
    )
    return (response.text or "No he podido explicar la respuesta anterior.").strip()


def _blocked_view(
    view_name: str,
    view_queries: dict[str, str],
    blocked_sources: set[str],
    memo: dict[str, bool],
    visiting: set[str],
) -> bool:
    """Detect direct and indirect dependencies on blocked source tables."""
    if view_name in memo:
        return memo[view_name]
    if view_name in visiting:
        return False
    visiting.add(view_name)
    query = view_queries.get(view_name, "").lower()
    blocked = any(
        re.search(rf"\b{re.escape(source)}\b", query)
        for source in blocked_sources
    )
    if not blocked:
        blocked = any(
            other != view_name
            and re.search(rf"\b{re.escape(other.lower())}\b", query)
            and _blocked_view(other, view_queries, blocked_sources, memo, visiting)
            for other in view_queries
        )
    visiting.remove(view_name)
    memo[view_name] = blocked
    return blocked


def dataset_schema() -> tuple[str, set[str]]:
    """Return schema and allowlist for views without blocked dependencies."""
    global _schema_cache
    now = time.monotonic()
    if _schema_cache and now - _schema_cache[0] < SCHEMA_CACHE_SECONDS:
        return _schema_cache[1], _schema_cache[2]

    lines = []
    views = {}
    external_sources = set()
    for item in bq.list_tables(f"{PROJECT_ID}.{DATASET_ID}"):
        table = bq.get_table(item.reference)
        if table.external_data_configuration is not None:
            external_sources.add(item.table_id.lower())
        if item.table_type == "VIEW":
            views[item.table_id] = table

    view_queries = {name: table.view_query or "" for name, table in views.items()}
    blocked_sources = BLOCKED_SOURCE_TABLES | external_sources
    memo: dict[str, bool] = {}
    allowed_views = {
        name
        for name in views
        if name in CONFIGURED_VIEWS
        and not _blocked_view(name, view_queries, blocked_sources, memo, set())
    }
    logging.info("Vistas autorizadas: %s", sorted(allowed_views))
    for name in sorted(allowed_views):
        table = views[name]
        fields = ", ".join(
            f"{f.name} {f.field_type}"
            + (f" ({f.description})" if f.description else "")
            for f in table.schema
        )
        lines.append(
            f"VISTA `{PROJECT_ID}.{DATASET_ID}.{name}`\n"
            f"Uso de negocio: {VIEW_BUSINESS_CONTEXT.get(name, 'Sin descripción adicional.')}\n"
            f"Columnas: {fields}"
        )
    if not lines:
        raise RuntimeError("No se encontraron vistas autorizadas con la configuración actual.")
    schema_text = "\n\n".join(lines)
    _schema_cache = (now, schema_text, allowed_views)
    return schema_text, allowed_views


def extract_sql(text: str) -> str:
    match = re.search(
        r"```(?:(?:google)?sql)?\s*(.*?)```",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    sql = (match.group(1) if match else text).strip().rstrip(";").strip()
    # Gemini sometimes returns a bare language label before otherwise valid SQL.
    sql = re.sub(r"^(?:google)?sql\s*(?:\r?\n|(?=SELECT\b|WITH\b))", "", sql, flags=re.IGNORECASE).strip()
    return sql


def validate_sql(sql: str, allowed_views: set[str]) -> None:
    normalized = re.sub(r"\s+", " ", sql).strip()
    if ";" in normalized:
        raise ValueError("La consulta contiene más de una sentencia.")
    if not re.match(r"^(SELECT|WITH)\b", normalized, re.IGNORECASE):
        raise ValueError("Solo se permiten consultas SELECT.")
    if FORBIDDEN_SQL.search(normalized):
        raise ValueError("La consulta contiene una operación no permitida.")

    try:
        tree = parse_one(sql, read="bigquery")
    except Exception as exc:
        raise ValueError("La consulta generada no es GoogleSQL válido.") from exc
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise ValueError("Solo se permiten consultas de lectura.")

    tables = list(tree.find_all(exp.Table))
    if not tables:
        raise ValueError("La consulta no contiene ninguna vista autorizada.")
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    authorized_view_found = False
    for table in tables:
        if not table.catalog and not table.db and table.name in cte_names:
            continue
        if (
            table.catalog != PROJECT_ID
            or table.db != DATASET_ID
            or table.name not in allowed_views
        ):
            raise ValueError("La consulta intenta acceder a una tabla o dataset no autorizado.")
        authorized_view_found = True
    if not authorized_view_found:
        raise ValueError("La consulta no contiene ninguna vista autorizada.")


def deterministic_stop_summary_sql(question: str, history: list[dict[str, str]]) -> str | None:
    """Build stable machine summaries after deduplicating stop identifiers."""
    normalized = lexical_intent_text(question)
    stop_terms = r"(?:paradas?|incidencias?|averias?|tiempos? muertos?|detenciones?|interrupciones?)"
    if not re.search(rf"\bresumen\b.*\b{stop_terms}\b|\b{stop_terms}\b.*\bresumen\b", normalized):
        return None

    filters = active_filters(question, history)
    predicates: list[str] = []
    if filters.get("fecha_desde"):
        predicates.append(
            f"fecha_operativa BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    if filters.get("maquina"):
        predicates.append(f"UPPER(TRIM(maquina)) = '{filters['maquina']}'")
    where_clause = " AND\n      ".join(predicates) if predicates else "TRUE"

    return f"""
WITH paradas_consolidadas AS (
  SELECT
    id_parada,
    maquina,
    oee,
    SUM(IFNULL(tiempo_parada_min, 0)) AS tiempo_parada_min
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_import_paradas`
  WHERE {where_clause}
  GROUP BY id_parada, maquina, oee
)
SELECT
  maquina,
  COUNT(*) AS total_paradas,
  SUM(tiempo_parada_min) AS minutos_totales,
  COUNTIF(UPPER(TRIM(oee)) = 'SI') AS paradas_no_planificadas,
  SUM(IF(UPPER(TRIM(oee)) = 'SI', tiempo_parada_min, 0)) AS minutos_no_planificados,
  COUNTIF(UPPER(TRIM(oee)) = 'NO') AS paradas_planificadas,
  SUM(IF(UPPER(TRIM(oee)) = 'NO', tiempo_parada_min, 0)) AS minutos_planificados
FROM paradas_consolidadas
GROUP BY maquina
ORDER BY maquina
LIMIT {MAX_RESULT_ROWS}
""".strip()


def deterministic_top_stops_sql(question: str, history: list[dict[str, str]]) -> str | None:
    """Build top-stop queries deterministically so deduplication precedes LIMIT."""
    normalized = lexical_intent_text(question)
    if not re.search(
        r"\b(?:paradas?|incidencias?|averias?|tiempos? muertos?|detenciones?|interrupciones?)\b"
        r".*\b(?:mas\s+larg[oa]s|mayores?\s+duracion)\b",
        normalized,
    ):
        return None

    word_limits = {
        "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
        "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    }
    requested = re.search(
        r"\b(?:top\s*|las?\s+)(\d{1,3})\s+"
        r"(?:paradas?|incidencias?|averias?|tiempos? muertos?|detenciones?|interrupciones?)\b",
        normalized,
    )
    limit = int(requested.group(1)) if requested else 10
    for word, value in word_limits.items():
        if re.search(
            rf"\b{word}\s+(?:paradas?|incidencias?|averias?|tiempos? muertos?|detenciones?|interrupciones?)\b",
            normalized,
        ):
            limit = value
            break
    limit = max(1, min(limit, MAX_RESULT_ROWS))

    filters = active_filters(question, history)
    predicates = []
    if filters.get("fecha_desde"):
        predicates.append(
            f"fecha_operativa BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    if filters.get("maquina"):
        predicates.append(f"UPPER(TRIM(maquina)) = '{filters['maquina']}'")

    if re.search(r"\b(?:no\s+planificadas?|oee)\b", normalized):
        predicates.append("UPPER(TRIM(oee)) = 'SI'")
    elif re.search(r"\bplanificadas?\b", normalized):
        predicates.append("UPPER(TRIM(oee)) = 'NO'")

    where_clause = " AND\n      ".join(predicates) if predicates else "TRUE"
    return f"""
WITH incidencias_consolidadas AS (
  SELECT
    id_parada,
    STRING_AGG(DISTINCT maquina, ', ' ORDER BY maquina) AS maquina,
    MIN(fecha_operativa) AS fecha_operativa,
    MIN(inicio_real) AS inicio_real,
    MAX(fin_real) AS fin_real,
    STRING_AGG(DISTINCT tipo_incidencia, ' | ' ORDER BY tipo_incidencia) AS tipo_incidencia,
    SUM(IFNULL(tiempo_parada_min, 0)) AS tiempo_parada_min,
    STRING_AGG(DISTINCT oee, ', ' ORDER BY oee) AS oee,
    COUNT(*) AS tramos_consolidados
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_import_paradas`
  WHERE {where_clause}
  GROUP BY id_parada
)
SELECT
  id_parada,
  maquina,
  inicio_real,
  fin_real,
  tipo_incidencia,
  tiempo_parada_min,
  oee,
  tramos_consolidados
FROM incidencias_consolidadas
ORDER BY tiempo_parada_min DESC, id_parada
LIMIT {limit}
""".strip()


def deterministic_stop_details_sql(question: str, history: list[dict[str, str]]) -> str | None:
    """List individual stops from common plant wording, always deduplicated by stop id."""
    normalized = lexical_intent_text(question)
    if not re.search(
        r"\b(?:paradas?|incidencias?|averias?|tiempos? muertos?|detenciones?|interrupciones?)\b",
        normalized,
    ):
        return None
    if re.search(
        r"\b(?:resumen|cuantas?|cuantos?|total|acumul|compara|comparar|por turno|"
        r"tipos?|causas?|motivos?|mas largas?|mayor duracion|ranking|top)\b",
        normalized,
    ):
        return None
    if not re.search(r"\b(?:que|cuales|dame|lista|detalle|hubo|tuvo|tuvieron|registr)\w*\b", normalized):
        return None

    filters = active_filters(question, history)
    predicates: list[str] = []
    if filters.get("fecha_desde"):
        predicates.append(
            f"fecha_operativa BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    if filters.get("maquina"):
        predicates.append(f"UPPER(TRIM(maquina)) = '{filters['maquina']}'")
    if re.search(r"\baverias?\b", normalized):
        predicates.append("REGEXP_CONTAINS(UPPER(tipo_incidencia), r'AVER[IÍ]A')")
    if re.search(r"\b(?:no planificadas?|oee)\b", normalized):
        predicates.append("UPPER(TRIM(oee)) = 'SI'")
    elif re.search(r"\bplanificadas?\b", normalized):
        predicates.append("UPPER(TRIM(oee)) = 'NO'")
    where_clause = " AND\n      ".join(predicates) if predicates else "TRUE"

    return f"""
WITH paradas_consolidadas AS (
  SELECT
    id_parada,
    STRING_AGG(DISTINCT maquina, ', ' ORDER BY maquina) AS maquina,
    MIN(fecha_operativa) AS fecha_operativa,
    MIN(inicio_real) AS inicio_real,
    MAX(fin_real) AS fin_real,
    STRING_AGG(DISTINCT tipo_incidencia, ' | ' ORDER BY tipo_incidencia) AS tipo_incidencia,
    SUM(IFNULL(tiempo_parada_min, 0)) AS tiempo_parada_min,
    STRING_AGG(DISTINCT oee, ', ' ORDER BY oee) AS oee,
    STRING_AGG(DISTINCT turno, ', ' ORDER BY turno) AS turno,
    STRING_AGG(DISTINCT operario, ', ' ORDER BY operario) AS operario,
    STRING_AGG(DISTINCT pieza_afectada, ', ' ORDER BY pieza_afectada) AS pieza_afectada,
    COUNT(*) AS tramos_consolidados
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_import_paradas`
  WHERE {where_clause}
  GROUP BY id_parada
)
SELECT
  id_parada,
  maquina,
  inicio_real,
  fin_real,
  tipo_incidencia,
  tiempo_parada_min,
  oee,
  turno,
  operario,
  pieza_afectada,
  tramos_consolidados
FROM paradas_consolidadas
ORDER BY fecha_operativa, inicio_real, id_parada
LIMIT {MAX_RESULT_ROWS}
""".strip()


def deterministic_oee_target_savings_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Build a stable historical/period aggregate for hypothetical OEE savings."""
    if not is_oee_target_simulation(question):
        return None
    target_percent = extract_oee_target_pct(question)
    if target_percent is None:
        return None
    target_ratio = target_percent / 100.0
    filters = active_filters(question, history)
    predicates: list[str] = []
    period_text = "todo el histórico disponible"
    if filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
        period_text = f"la semana {filters['semana']}"
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' AND DATE '{filters['fecha_hasta']}'"
        )
        period_text = (
            filters["fecha_desde"]
            if filters["fecha_desde"] == filters.get("fecha_hasta")
            else f"{filters['fecha_desde']} a {filters['fecha_hasta']}"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
        period_text = f"el año {filters['anio']}"

    machine = filters.get("maquina")
    article = filters.get("articulo")
    if machine:
        predicates.append(f"UPPER(TRIM(maquina)) = '{machine}'")
    if article:
        predicates.append(f"UPPER(TRIM(articulo)) = '{article}'")
    where_clause = "\n  WHERE " + " AND\n    ".join(predicates) if predicates else ""
    machine_sql = f"'{machine}' AS maquina" if machine else "CAST(NULL AS STRING) AS maquina"
    article_sql = f"'{article}' AS articulo" if article else "CAST(NULL AS STRING) AS articulo"

    return f"""
WITH agregados AS (
  SELECT
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(tiempo_erp, 0)) AS tiempo_erp,
    SUM(IFNULL(h_teoricas, 0)) AS h_teoricas,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`{where_clause}
),
componentes AS (
  SELECT
    total_piezas_producidas,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct,
    GREATEST(
      ((tiempo_plan - tiempo_erp_capado)
        + (tiempo_erp - h_teoricas)
        + IFNULL(SAFE_DIVIDE(reoperar, total_piezas_producidas) * tiempo_erp, 0)) * 24.9,
      0
    ) AS perdida_actual_oee_eur
  FROM agregados
),
escenario AS (
  SELECT
    *,
    calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_actual_pct,
    {target_ratio:.6f} AS oee_objetivo_pct
  FROM componentes
)
SELECT
  {machine_sql},
  {article_sql},
  '{period_text}' AS periodo,
  total_piezas_producidas,
  calidad_pct,
  disponibilidad_pct,
  rendimiento_pct,
  oee_actual_pct,
  oee_objetivo_pct,
  perdida_actual_oee_eur,
  CASE
    WHEN oee_actual_pct IS NULL OR oee_actual_pct >= 1 OR oee_objetivo_pct <= oee_actual_pct THEN 0
    ELSE perdida_actual_oee_eur * LEAST(
      GREATEST(SAFE_DIVIDE(oee_objetivo_pct - oee_actual_pct, 1 - oee_actual_pct), 0),
      1
    )
  END AS ahorro_estimado_eur,
  perdida_actual_oee_eur - CASE
    WHEN oee_actual_pct IS NULL OR oee_actual_pct >= 1 OR oee_objetivo_pct <= oee_actual_pct THEN 0
    ELSE perdida_actual_oee_eur * LEAST(
      GREATEST(SAFE_DIVIDE(oee_objetivo_pct - oee_actual_pct, 1 - oee_actual_pct), 0),
      1
    )
  END AS perdida_restante_estimada_eur,
  'cierre_proporcional_de_la_brecha_hasta_oee_100' AS hipotesis_calculo
FROM escenario
LIMIT 1
""".strip()


def deterministic_machine_metric_difference_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Calculate percentage-point differences between two explicitly named robots."""
    normalized = normalized_business_text(question)
    machines = []
    for match in re.finditer(r"\bRB\s*[-_ ]?\s*(\d+)\b", question, re.IGNORECASE):
        machine = f"RB{match.group(1)}"
        if machine not in machines:
            machines.append(machine)
    if len(machines) != 2 or not re.search(
        r"\b(?:puntos?|diferencia|separan?|distancia|brecha)\b",
        normalized,
    ):
        return None

    metric = None
    metric_labels = {
        "rendimiento": "rendimiento",
        "disponibilidad": "disponibilidad",
        "calidad": "calidad",
        "oee": "OEE",
    }
    context_texts = [question]
    for turn in reversed(history[-6:]):
        context_texts.extend((str(turn.get("user", "")), str(turn.get("assistant", ""))))
    for text in context_texts:
        candidate = normalized_business_text(text)
        match = re.search(r"\b(rendimiento|disponibilidad|calidad|oee|ooe)\b", candidate)
        if match:
            metric = "oee" if match.group(1) == "ooe" else match.group(1)
            break
    if metric is None:
        return None

    filters = active_filters(question, history)
    predicates = [f"UPPER(TRIM(maquina)) IN ('{machines[0]}', '{machines[1]}')"]
    period_text = "el periodo solicitado"
    if filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
        period_text = f"la semana {filters['semana']}"
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' AND DATE '{filters['fecha_hasta']}'"
        )
        period_text = (
            filters["fecha_desde"]
            if filters["fecha_desde"] == filters.get("fecha_hasta")
            else f"{filters['fecha_desde']} a {filters['fecha_hasta']}"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
        period_text = filters["anio"]
    else:
        return None

    metric_column = {
        "calidad": "calidad_pct",
        "disponibilidad": "disponibilidad_pct",
        "rendimiento": "rendimiento_pct",
        "oee": "oee_pct",
    }[metric]
    where_clause = " AND\n      ".join(predicates)
    return f"""
WITH agregados AS (
  SELECT
    UPPER(TRIM(maquina)) AS maquina,
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
  WHERE {where_clause}
  GROUP BY maquina
),
indicadores AS (
  SELECT
    maquina,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct
  FROM agregados
),
metricas AS (
  SELECT
    maquina,
    calidad_pct,
    disponibilidad_pct,
    rendimiento_pct,
    calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct
  FROM indicadores
),
comparacion AS (
  SELECT
    MAX(IF(maquina = '{machines[0]}', {metric_column}, NULL)) AS valor_maquina_1_pct,
    MAX(IF(maquina = '{machines[1]}', {metric_column}, NULL)) AS valor_maquina_2_pct
  FROM metricas
)
SELECT
  '{machines[0]}' AS maquina_1,
  '{machines[1]}' AS maquina_2,
  '{metric_labels[metric]}' AS metrica,
  '{period_text}' AS periodo,
  valor_maquina_1_pct,
  valor_maquina_2_pct,
  ABS(valor_maquina_1_pct - valor_maquina_2_pct) * 100 AS diferencia_puntos_porcentuales
FROM comparacion
LIMIT 1
""".strip()


def deterministic_single_kpi_sql(question: str, history: list[dict[str, str]]) -> str | None:
    """Calculate one machine/article KPI aggregate without delegating filters to the LLM."""
    normalized = lexical_intent_text(question)
    filters = active_filters(question, history)

    # Rankings, comparisons and breakdowns need a variable number of result rows.
    # They remain in the general SQL generator.
    if filters.get("modo_consulta") in {"por_articulo", "global_y_por_articulo", "paradas"}:
        return None
    if re.search(
        r"\b(?:mejor|peor|ranking|compara|comparar|todos|todas|segunda?|tercera?|top)\b",
        normalized,
    ):
        return None
    if not filters.get("maquina"):
        return None
    if not (filters.get("semana") or filters.get("fecha_desde")):
        return None
    asks_follow_up_details = bool(re.search(
        r"\b(?:dame|quiero|muestra(?:me)?|amplia|ampliame)\b.{0,25}\b(?:mas\s+)?detalles?\b|"
        r"\bsin\s+cambiar\s+el\s+periodo\b",
        normalized,
    ))
    if not asks_follow_up_details and not re.search(
        r"\b(?:oee|ooe|calidad|disponibilidad|rendimiento|piezas|producci[oó]n|p[eé]rdida)\b",
        normalized,
    ):
        return None

    predicates: list[str] = []
    if filters.get("semana"):
        # The dataset's business week is authoritative. Never derive it from dates.
        predicates.append(f"semana = '{filters['semana']}'")
    else:
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' AND DATE '{filters['fecha_hasta']}'"
        )
    predicates.append(f"UPPER(TRIM(maquina)) = '{filters['maquina']}'")
    if filters.get("articulo"):
        predicates.append(f"UPPER(TRIM(articulo)) = '{filters['articulo']}'")
    where_clause = " AND\n      ".join(predicates)

    return f"""
WITH agregados AS (
  SELECT
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(tiempo_erp, 0)) AS tiempo_erp,
    SUM(IFNULL(h_teoricas, 0)) AS h_teoricas,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
  WHERE {where_clause}
),
indicadores AS (
  SELECT
    total_piezas_producidas,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct,
    ((tiempo_plan - tiempo_erp_capado)
      + (tiempo_erp - h_teoricas)
      + IFNULL(SAFE_DIVIDE(reoperar, total_piezas_producidas) * tiempo_erp, 0)) * 24.9
      AS perdida_oee_eur
  FROM agregados
)
SELECT
  total_piezas_producidas,
  calidad_pct,
  disponibilidad_pct,
  rendimiento_pct,
  calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct,
  perdida_oee_eur
FROM indicadores
LIMIT 1
""".strip()


def _asks_availability_component_breakdown(question: str) -> bool:
    normalized = lexical_intent_text(question)
    return bool(
        re.search(r"\bdisponibilidad\b", normalized)
        and re.search(r"\b(?:desglos\w*|componentes?|descompon\w*|calculo)\b", normalized)
    )


def deterministic_availability_breakdown_sql(
    question: str, history: list[dict[str, str]]
) -> str | None:
    """Return only the availability inputs exposed by the OEE master view."""
    if not _asks_availability_component_breakdown(question):
        return None
    filters = active_filters(question, history)
    if not filters.get("maquina"):
        return None
    predicates = [f"UPPER(TRIM(maquina)) = '{filters['maquina']}'"]
    if filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
    if filters.get("articulo"):
        predicates.append(f"UPPER(TRIM(articulo)) = '{filters['articulo']}'")
    where_clause = " AND\n      ".join(predicates)
    return f"""
SELECT
  COUNT(*) AS filas_fuente,
  SUM(tiempo_plan) AS tiempo_plan,
  SUM(tiempo_erp_capado) AS tiempo_erp_capado,
  CASE
    WHEN SUM(tiempo_plan) = 0 THEN NULL
    ELSE SUM(tiempo_erp_capado) / SUM(tiempo_plan)
  END AS disponibilidad_pct
FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
WHERE {where_clause}
LIMIT 1
""".strip()


def is_causal_availability_question(question: str) -> bool:
    """Match causal questions about low availability without capturing generic why questions."""
    normalized = lexical_intent_text(question)
    asks_why = bool(re.search(r"\b(?:por\s+que|explica\w*|causa\w*|motivo\w*)\b", normalized))
    mentions_availability = bool(re.search(r"\bdisponibilidad\b", normalized))
    low_availability = bool(re.search(r"\b(?:baj\w*|reducid\w*|escas\w*|limitad\w*)\b", normalized))
    return asks_why and mentions_availability and low_availability


def _causal_period_is_explicit(text: str) -> bool:
    normalized = normalized_business_text(text)
    return bool(
        resolve_temporal_context(text).get("fecha_desde")
        or re.search(r"\b20\d{2}\s*[-/]\s*\d{1,2}\b", text)
        or re.search(r"\b(?:semana|s)\s*[-_/ ]?\s*\d{1,2}\b", text, re.I)
        or re.search(r"\b20\d{2}\b", normalized)
        or re.search(
            r"\b(?:historico|historica|historicamente|todo\s+el\s+historico|"
            r"todos\s+los\s+datos|periodo\s+completo)\b",
            normalized,
        )
    )


def causal_availability_filters(
    question: str, history: list[dict[str, str]]
) -> dict[str, str]:
    """Apply stricter, causal-only scope rules without changing global filter inheritance."""
    filters = active_filters(question, history)
    explicit_current_scope = _causal_period_is_explicit(question)
    if not explicit_current_scope:
        latest = history[-1] if history else {}
        latest_question = str(latest.get("user", ""))
        latest_answer = str(latest.get("assistant", ""))
        latest_has_oee_scope = (
            _conversation_domain(latest_question) == "oee"
            and _causal_period_is_explicit(latest_question + " " + latest_answer)
        )
        inherited_historical_scope = filters.get("alcance_temporal") == "historico"
        if not latest_has_oee_scope or inherited_historical_scope:
            for key in ("fecha_desde", "fecha_hasta", "semana", "anio", "alcance_temporal"):
                filters.pop(key, None)
            filters["requiere_aclaracion"] = "periodo"
    return filters


def causal_availability_period_label(filters: dict[str, str]) -> str | None:
    if filters.get("fecha_desde"):
        start, end = filters["fecha_desde"], filters.get("fecha_hasta", filters["fecha_desde"])
        return start if start == end else f"{start}–{end}"
    if filters.get("semana"):
        return f"semana ISO {filters['semana']}"
    if filters.get("anio"):
        return f"año {filters['anio']}"
    if filters.get("alcance_temporal") == "historico":
        return "todo el histórico solicitado"
    return None


def deterministic_causal_availability_sql(question: str, history: list[dict[str, str]]) -> tuple[str, str] | None:
    """Build separate OEE and deduplicated stop summaries for one explicit scope."""
    if not is_causal_availability_question(question):
        return None
    filters = causal_availability_filters(question, history)
    machine = filters.get("maquina")
    if not machine or filters.get("maquinas") or filters.get("requiere_aclaracion"):
        return None
    predicates = [f"UPPER(TRIM(maquina)) = '{machine}'"]
    stop_predicates = [f"UPPER(TRIM(maquina)) = '{machine}'"]
    if filters.get("fecha_desde"):
        start, end = filters["fecha_desde"], filters["fecha_hasta"]
        predicates.append(f"fecha BETWEEN DATE '{start}' AND DATE '{end}'")
        stop_predicates.append(f"fecha_operativa BETWEEN DATE '{start}' AND DATE '{end}'")
    elif filters.get("semana"):
        year, week = (int(part) for part in filters["semana"].split("-"))
        start = date.fromisocalendar(year, week, 1).isoformat()
        end = date.fromisocalendar(year, week, 7).isoformat()
        predicates.append(f"fecha BETWEEN DATE '{start}' AND DATE '{end}'")
        stop_predicates.append(f"fecha_operativa BETWEEN DATE '{start}' AND DATE '{end}'")
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
        stop_predicates.append(f"EXTRACT(YEAR FROM fecha_operativa) = {int(filters['anio'])}")
    elif filters.get("alcance_temporal") != "historico":
        return None
    oee_sql = f"""
SELECT COUNT(*) AS filas_fuente, SUM(tiempo_plan) AS tiempo_plan,
  SUM(tiempo_erp_capado) AS tiempo_erp_capado,
  CASE WHEN SUM(tiempo_plan) = 0 THEN NULL
       ELSE SUM(tiempo_erp_capado) / SUM(tiempo_plan) END AS disponibilidad_pct
FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
WHERE {' AND '.join(predicates)}
LIMIT 1
""".strip()
    stop_sql = f"""
WITH paradas_por_id AS (
  SELECT id_parada,
    COUNTIF(UPPER(TRIM(oee)) = 'SI') AS etiquetas_si,
    COUNTIF(UPPER(TRIM(oee)) = 'NO') AS etiquetas_no,
    COUNTIF(oee IS NULL OR UPPER(TRIM(oee)) NOT IN ('SI', 'NO')) AS etiquetas_desconocidas,
    COUNTIF(tiempo_parada_min IS NULL) AS tramos_minutos_desconocidos,
    SUM(tiempo_parada_min) AS minutos_conocidos
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_import_paradas`
  WHERE {' AND '.join(stop_predicates)}
  GROUP BY id_parada
), paradas_consolidadas AS (
  SELECT id_parada,
    CASE
      WHEN etiquetas_si > 0 AND etiquetas_no = 0 AND etiquetas_desconocidas = 0 THEN 'SI'
      WHEN etiquetas_no > 0 AND etiquetas_si = 0 AND etiquetas_desconocidas = 0 THEN 'NO'
      WHEN etiquetas_si > 0 AND etiquetas_no > 0 THEN 'INCONSISTENTE'
      ELSE 'DESCONOCIDA'
    END AS clasificacion_oee,
    tramos_minutos_desconocidos,
    minutos_conocidos
  FROM paradas_por_id
)
SELECT COUNT(*) AS total_paradas,
  COUNTIF(clasificacion_oee = 'SI') AS paradas_oee_si,
  SUM(IF(clasificacion_oee = 'SI' AND tramos_minutos_desconocidos = 0, minutos_conocidos, NULL)) AS tiempo_oee_si,
  COUNTIF(clasificacion_oee = 'SI' AND tramos_minutos_desconocidos > 0) AS paradas_si_minutos_desconocidos,
  COUNTIF(clasificacion_oee = 'NO') AS paradas_oee_no,
  SUM(IF(clasificacion_oee = 'NO' AND tramos_minutos_desconocidos = 0, minutos_conocidos, NULL)) AS tiempo_oee_no,
  COUNTIF(clasificacion_oee = 'NO' AND tramos_minutos_desconocidos > 0) AS paradas_no_minutos_desconocidos,
  COUNTIF(clasificacion_oee = 'DESCONOCIDA') AS paradas_oee_desconocidas,
  SUM(IF(clasificacion_oee = 'DESCONOCIDA' AND tramos_minutos_desconocidos = 0, minutos_conocidos, NULL)) AS tiempo_oee_desconocido,
  COUNTIF(clasificacion_oee = 'DESCONOCIDA' AND tramos_minutos_desconocidos > 0) AS paradas_desconocidas_minutos_null,
  COUNTIF(clasificacion_oee = 'INCONSISTENTE') AS paradas_oee_inconsistentes,
  SUM(IF(clasificacion_oee = 'INCONSISTENTE' AND tramos_minutos_desconocidos = 0, minutos_conocidos, NULL)) AS tiempo_oee_inconsistente,
  COUNTIF(clasificacion_oee = 'INCONSISTENTE' AND tramos_minutos_desconocidos > 0) AS paradas_inconsistentes_minutos_null
FROM paradas_consolidadas
LIMIT 1
""".strip()
    return oee_sql, stop_sql


def deterministic_causal_availability_answer(
    oee_rows, stop_rows, oee_error=None, stop_error=None, machine=None, period=None
) -> str:
    """Present verified aggregates and stop labels without asserting quantitative causality."""
    def fmt(value):
        if value is None:
            return "NULL"
        try:
            return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(value)
    def fmt_count(value):
        if value is None:
            return "NULL"
        try:
            return f"{int(value):,}".replace(",", ".")
        except (TypeError, ValueError):
            return str(value)
    scope = "Alcance aplicado: "
    scope += f"máquina {machine}" if machine else "máquina no confirmada"
    scope += f"; periodo {period}" if period else "; periodo no confirmado"
    sections = [scope, "Datos comprobados"]
    if oee_error:
        sections.append(f"La consulta OEE falló ({oee_error}); no hay agregados disponibles.")
    elif not oee_rows:
        sections.append("La consulta OEE no devolvió resultado; no se puede confirmar disponibilidad.")
    else:
        row = oee_rows[0]
        availability = row.get("disponibilidad_pct")
        pct = "NULL" if availability is None else f"{fmt(float(availability) * 100)} %"
        sections.append(f"Filas OEE: {fmt(row.get('filas_fuente'))}; SUM(tiempo_plan): {fmt(row.get('tiempo_plan'))}; SUM(tiempo_erp_capado): {fmt(row.get('tiempo_erp_capado'))}; disponibilidad = SUM(tiempo_erp_capado) / SUM(tiempo_plan): {pct}. La unidad de tiempo de estos campos no está documentada en el esquema.")
    sections.append("Paradas registradas")
    if stop_error:
        sections.append(f"La consulta de paradas falló ({stop_error}); no se puede determinar si hubo registros.")
    elif not stop_rows:
        sections.append("La consulta de paradas no devolvió resultado; no se puede determinar si hubo registros.")
    else:
        row = stop_rows[0]
        if row.get("total_paradas") == 0:
            sections.append("No hay paradas registradas en la consulta para ese alcance; esto no demuestra que no hubiera pérdidas.")
        elif row.get("total_paradas") is None:
            sections.append("El recuento de paradas no está disponible; no se puede determinar si hubo registros.")
        else:
            def category_details(label, count_key, minutes_key, unknown_key):
                count = row.get(count_key)
                if count is None:
                    return f"{label}: recuento no disponible."
                if int(count) == 0:
                    return f"{label}: 0 incidencias."
                return (
                    f"{label}: {fmt_count(count)} incidencias; suma de minutos de incidencias "
                    f"con todos sus tramos conocidos: {fmt(row.get(minutes_key))}; "
                    f"duración desconocida por minutos NULL en "
                    f"{fmt_count(row.get(unknown_key))} incidencias."
                )

            category_text = " ".join((
                category_details("oee='SI'", "paradas_oee_si", "tiempo_oee_si", "paradas_si_minutos_desconocidos"),
                category_details("oee='NO'", "paradas_oee_no", "tiempo_oee_no", "paradas_no_minutos_desconocidos"),
                category_details("Etiqueta desconocida/NULL", "paradas_oee_desconocidas", "tiempo_oee_desconocido", "paradas_desconocidas_minutos_null"),
                category_details("Etiquetas inconsistentes", "paradas_oee_inconsistentes", "tiempo_oee_inconsistente", "paradas_inconsistentes_minutos_null"),
            ))
            sections.append(
                f"Incidencias deduplicadas: {fmt_count(row.get('total_paradas'))}. {category_text}"
            )
    sections.extend(("Qué puede concluirse", "La disponibilidad refleja únicamente la fórmula corporativa. Las etiquetas oee='SI'/'NO' clasifican paradas, pero no cuantifican por sí solas su efecto sobre la disponibilidad; una incidencia con etiquetas mixtas no se asigna a SI ni a NO, y no se atribuye causalidad a incidencias concretas."))
    return "\n\n".join(sections)


def deterministic_availability_breakdown_answer(
    question: str,
    rows: list[dict],
    sql: str,
    history: list[dict[str, str]],
) -> str | None:
    """Report available OEE aggregates and state which requested components are not proven."""
    expected_sql = deterministic_availability_breakdown_sql(question, history)
    if not expected_sql or not sql or sql.strip() != expected_sql.strip():
        return None
    if not rows:
        return (
            "La consulta de desglose no devolvió una fila de resultado. No puedo distinguir "
            "con seguridad si faltan datos o si el resultado está incompleto; no estimaré tiempos."
        )
    row = rows[0]
    required_fields = {
        "filas_fuente", "tiempo_plan", "tiempo_erp_capado", "disponibilidad_pct"
    }
    if not required_fields.issubset(row):
        return None

    def format_value(value):
        if value is None:
            return "no disponible"
        try:
            return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(value)

    try:
        source_rows = int(row["filas_fuente"])
    except (TypeError, ValueError):
        return None
    if source_rows == 0:
        return (
            "No hay filas de origen para la máquina y el periodo consultados. "
            "Las sumas y la disponibilidad quedan NULL; no hay tiempos que pueda desglosar "
            "y no los estimaré."
        )

    details = []
    for label, field in (
        ("SUM(tiempo_plan)", "tiempo_plan"),
        ("SUM(tiempo_erp_capado)", "tiempo_erp_capado"),
    ):
        value = row[field]
        details.append(f"{label} = {format_value(value)}")

    planned = row["tiempo_plan"]
    numerator = row["tiempo_erp_capado"]
    availability = row["disponibilidad_pct"]
    if planned is None:
        status = "SUM(tiempo_plan) es NULL, por lo que la fórmula devuelve NULL."
    else:
        try:
            planned_value = float(planned)
        except (TypeError, ValueError):
            return None
        if planned_value == 0:
            status = "SUM(tiempo_plan) es cero, por lo que la fórmula corporativa devuelve NULL."
        elif numerator is None:
            status = "SUM(tiempo_erp_capado) es NULL; no se sustituye por cero y la disponibilidad es NULL."
        elif availability is None:
            status = "La fórmula corporativa no produjo una disponibilidad numérica."
        else:
            status = (
                "Disponibilidad calculada sobre los totales: "
                f"{float(availability) * 100:.2f}%."
            )
    return (
        "Agregados devueltos (sin unidad especificada en el esquema): " + "; ".join(details) + ". "
        + status + " La vista define disponibilidad como "
        "CASE WHEN SUM(tiempo_plan) = 0 THEN NULL ELSE "
        "SUM(tiempo_erp_capado) / SUM(tiempo_plan) END; "
        "pero no permite identificar por separado un tiempo operativo ni reconciliar la "
        "duración de paradas OEE con ese indicador. vw_oee_master ya integra y prorratea "
        "paradas, mientras vw_import_paradas ofrece incidencias por separado; sumar o "
        "restar sus duraciones aquí no sería verificable. No estimaré los componentes ausentes."
    )


def planner_requested_weeks(question: str) -> list[int]:
    """Extract one or more planning-week numbers without treating the year as a week."""
    if re.search(
        r"\b(?:la\s+)?semana\s+(?:que\s+(?:viene|entra)|siguiente|proxima|pr[oó]xima)\b|"
        r"\b(?:pr[oó]xima|siguiente)\s+semana\b",
        question,
        re.IGNORECASE,
    ):
        next_week = datetime.now(MADRID_TZ).date() + timedelta(days=7)
        return [next_week.isocalendar().week]
    explicit = re.findall(r"\b20\d{2}\s*[-/]\s*(\d{1,2})\b", question)
    if explicit:
        return sorted({int(value) for value in explicit if 1 <= int(value) <= 53})

    short = re.findall(r"\bs\s*[-_/]?\s*(\d{1,2})\b", question, re.IGNORECASE)
    if short:
        return sorted({int(value) for value in short if 1 <= int(value) <= 53})

    match = re.search(
        r"\bsemanas?\s+((?:\d{1,2}\s*(?:(?:,|y|e|-)\s*)?)+)",
        question,
        re.IGNORECASE,
    )
    if not match:
        return []
    return sorted({
        int(value)
        for value in re.findall(r"\d{1,2}", match.group(1))
        if 1 <= int(value) <= 53
    })


def planner_requested_iso_periods(question: str) -> list[tuple[int, int]]:
    """Extract validated YYYY-WW pairs without collapsing weeks from different years."""
    periods = []
    for year_text, week_text in re.findall(
        r"\b(20\d{2})\s*[-/]\s*(\d{1,2})\b(?!\s*[-/]\s*\d{1,2}\b)",
        question,
    ):
        year, week = int(year_text), int(week_text)
        try:
            date.fromisocalendar(year, week, 1)
        except ValueError:
            continue
        period = (year, week)
        if period not in periods:
            periods.append(period)
    return periods


def has_planner_load_comparison_intent(
    question: str, history: list[dict[str, str]]
) -> bool:
    """Recover an elliptical comparison intent only inside planner context."""
    normalized = lexical_intent_text(question)
    comparison = r"\b(?:compara\w*|comparaci[oó]n|entre)\b"
    load = r"\b(?:carga pendiente|trabajo pendiente|backlog|cartera)\b"
    if re.search(comparison, normalized) and re.search(load, normalized):
        return True
    current_domain = _conversation_domain(question)
    if current_domain and current_domain != "planificador":
        return False
    for turn in reversed(history):
        prior_question = str(turn.get("user", ""))
        prior_domain = _conversation_domain(prior_question)
        if prior_domain and prior_domain != "planificador":
            return False
        prior_normalized = lexical_intent_text(prior_question)
        if re.search(comparison, prior_normalized) and re.search(load, prior_normalized):
            return True
    return False


def planner_context_weeks(question: str, history: list[dict[str, str]]) -> list[int]:
    """Resolve explicit planning weeks and demonstrative follow-ups such as 'esas semanas'."""
    weeks = planner_requested_weeks(question)
    if weeks:
        return weeks
    normalized = normalized_business_text(question)
    if not re.search(r"\b(?:esas|mismas|ambas|aquellas)\s+semanas\b", normalized):
        return []
    for turn in reversed(history):
        weeks = planner_requested_weeks(str(turn.get("user", "")))
        if weeks:
            return weeks
    return []


def deterministic_planner_article_load_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Rank planned articles by pending quantity."""
    normalized = lexical_intent_text(question)
    if not re.search(r"\b(?:referencias?|articulos?|piezas?|productos?)\b", normalized):
        return None
    if "pendiente" not in normalized or not re.search(r"\b(?:mas|mayor|cantidad|ranking|top)\b", normalized):
        return None
    return f"""
SELECT
  UPPER(TRIM(articulo)) AS articulo,
  SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente,
  COUNT(DISTINCT pev) AS ordenes,
  COUNT(DISTINCT maquina) AS robots
FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
WHERE EXTRACT(YEAR FROM fecha_necesidad) = {datetime.now(MADRID_TZ).year}
  AND IFNULL(cantidad_pendiente, 0) > 0
  AND articulo IS NOT NULL
GROUP BY articulo
ORDER BY cantidad_pendiente DESC, articulo
LIMIT 10
""".strip()


def deterministic_planner_week_load_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Aggregate pending workload by robot using the planner's business-week field."""
    normalized = lexical_intent_text(question)
    history_text = " ".join(str(turn.get("user", "")) for turn in history[-4:])
    intent_text = f"{normalized} {lexical_intent_text(history_text)}"
    asks_pending_load = bool(re.search(
        r"\b(carga pendiente|trabajo pendiente|backlog|cartera|pedidos? pendientes?|"
        r"ordenes?(?:\s+de\s+fabricacion)?.{0,12}pendientes?|cola de produccion|ocupacion|saturacion|sobrecarga|"
        r"saturados?|sobrecargados?|cargados?|turnos?\s+(?:ajustados?\s+por\s+oee|previstos?|planificados?|ocupados?))\b|"
        r"\bpendiente\b.*\b(robots?|equipos?)\b|"
        r"\bcargas?\b.*\b(robots?|equipos?|maquinas?|semanas?)\b",
        intent_text,
    ))
    # Decide the ranking metric from the current question.  Do not inherit it
    # from previous turns: a user may ask about saturation and immediately
    # afterwards ask which robot has the most pending units.
    asks_quantity_ranking = bool(re.search(
        r"\b(?:mas|mayor)\s+(?:cantidad\s+de\s+)?(?:carga|trabajo|cantidad|piezas?|unidades?)\s+pendientes?\b|"
        r"\b(?:carga|cantidad|piezas?|unidades?)\s+pendientes?\b.*\b(?:mas|mayor)\b",
        normalized,
    ))
    asks_turn_deviation = bool(re.search(
        r"\b(desviacion|desvio)\b.*\bturnos?\b|\bturnos?\b.*\b(desviacion|desvio)\b",
        normalized,
    ))
    asks_hour_deviation = bool(re.search(
        r"\b(desviacion|desvio)\b.*\bhoras?\b|\bhoras?\b.*\b(desviacion|desvio)\b",
        normalized,
    ))
    asks_saturation = bool(re.search(
        r"\b(?:saturacion|sobrecarga|saturados?|sobrecargados?|mas cargados?)\b",
        normalized,
    ))
    if not (
        asks_pending_load
        or asks_quantity_ranking
        or asks_turn_deviation
        or asks_hour_deviation
        or asks_saturation
    ):
        return None
    weeks = planner_context_weeks(question, history)
    explicit_iso_periods = planner_requested_iso_periods(question)
    is_two_week_load_comparison = bool(
        asks_pending_load
        and has_planner_load_comparison_intent(question, history)
        and len(explicit_iso_periods) == 2
    )
    # If no week is stated, allow a plant-wide current-year workload ranking.
    # This supports questions such as “¿qué robots están sobrecargados?”.
    year_match = re.search(r"\b(20\d{2})\b", question)
    year = int(year_match.group(1)) if year_match else datetime.now(MADRID_TZ).year
    if is_two_week_load_comparison:
        iso_period_predicates = [
            "(EXTRACT(ISOYEAR FROM fecha_necesidad) = "
            f"{period_year} AND CAST(semana_necesidad AS STRING) = '{period_week}')"
            for period_year, period_week in explicit_iso_periods
        ]
        planner_filters = ["(" + " OR ".join(iso_period_predicates) + ")"]
    else:
        planner_filters = [f"EXTRACT(YEAR FROM fecha_necesidad) = {year}"]
    if weeks and not is_two_week_load_comparison:
        planner_filters.append(
            f"CAST(semana_necesidad AS STRING) IN ({', '.join(repr(str(week)) for week in weeks)})"
        )
    # Use reconciled conversational filters so an elliptical period change
    # keeps the machine scope selected in the preceding planner request.
    filters = active_filters(question, history)
    machine_ids = [
        machine for machine in filters.get("maquinas", "").split(",")
        if re.fullmatch(r"RB\d+", machine)
    ]
    if machine_ids:
        machine_values = ", ".join(repr(machine) for machine in machine_ids)
        planner_filters.append(f"UPPER(TRIM(maquina)) IN ({machine_values})")
    else:
        machine = filters.get("maquina", "")
        if re.fullmatch(r"RB\d+", machine):
            planner_filters.append(f"UPPER(TRIM(maquina)) = '{machine}'")

    if asks_quantity_ranking:
        order_metric = "cantidad_pendiente DESC"
    elif asks_hour_deviation:
        order_metric = "horas_desviacion DESC"
    elif asks_turn_deviation or asks_saturation:
        order_metric = "turnos_desviacion DESC"
    else:
        order_metric = "cantidad_pendiente DESC"
    saturation_having = "\nHAVING turnos_desviacion > 0" if asks_saturation and not asks_quantity_ranking else ""
    if not weeks:
        return f"""
SELECT
  UPPER(TRIM(maquina)) AS maquina,
  COUNT(DISTINCT pev) AS ordenes,
  SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente,
  SUM(IFNULL(horas_totales_aplicadas, 0)) AS horas_totales_aplicadas,
  SUM(IFNULL(turnos_aplicados, 0)) AS turnos_aplicados,
  SUM(IFNULL(horas_desviacion, 0)) AS horas_desviacion,
  SUM(IFNULL(turnos_aplicados, 0))
    - SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion,
  MIN(fecha_necesidad) AS primera_fecha_necesidad,
  MAX(fecha_necesidad) AS ultima_fecha_necesidad
FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
WHERE {' AND '.join(planner_filters)}
  AND REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')
  AND IFNULL(cantidad_pendiente, 0) > 0
GROUP BY maquina{saturation_having}
ORDER BY {order_metric}, maquina
LIMIT {MAX_RESULT_ROWS}
""".strip()

    return f"""
SELECT
  {"EXTRACT(ISOYEAR FROM fecha_necesidad) AS anio_iso," if is_two_week_load_comparison else ""}
  semana_necesidad,
  UPPER(TRIM(maquina)) AS maquina,
  COUNT(DISTINCT pev) AS ordenes,
  SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente,
  SUM(IFNULL(horas_totales_aplicadas, 0)) AS horas_totales_aplicadas,
  SUM(IFNULL(turnos_aplicados, 0)) AS turnos_aplicados,
  SUM(IFNULL(horas_desviacion, 0)) AS horas_desviacion,
  SUM(IFNULL(turnos_aplicados, 0))
    - SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion,
  MIN(fecha_necesidad) AS primera_fecha_necesidad,
  MAX(fecha_necesidad) AS ultima_fecha_necesidad
FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
  WHERE {' AND '.join(planner_filters)}
  AND REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')
  AND IFNULL(cantidad_pendiente, 0) > 0
GROUP BY {"EXTRACT(ISOYEAR FROM fecha_necesidad), " if is_two_week_load_comparison else ""}semana_necesidad, maquina{saturation_having}
ORDER BY semana_necesidad, {order_metric}, maquina
LIMIT {MAX_RESULT_ROWS}
""".strip()


def planner_historical_oee_ctes() -> str:
    """Return the shared, corporate OEE calculation used to assess robot candidates."""
    return f"""
historico_agregado AS (
  SELECT
    UPPER(TRIM(articulo)) AS articulo,
    UPPER(TRIM(maquina)) AS maquina,
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
  WHERE REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')
  GROUP BY articulo, maquina
),
historico_indicadores AS (
  SELECT
    articulo,
    maquina,
    total_piezas_producidas,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct
  FROM historico_agregado
  WHERE total_piezas_producidas > 0
    AND tiempo_plan > 0
    AND piezas_teo > 0
),
historico AS (
  SELECT
    articulo,
    maquina,
    total_piezas_producidas,
    calidad_pct,
    disponibilidad_pct,
    rendimiento_pct,
    calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct
  FROM historico_indicadores
  WHERE calidad_pct IS NOT NULL
    AND disponibilidad_pct IS NOT NULL
    AND rendimiento_pct IS NOT NULL
),
carga_actual AS (
  SELECT
    UPPER(TRIM(maquina)) AS maquina,
    SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente_robot,
    SUM(IFNULL(horas_totales_aplicadas, 0)) AS horas_aplicadas_robot,
    SUM(IFNULL(horas_desviacion, 0)) AS horas_desviacion_robot,
    SUM(IFNULL(turnos_aplicados, 0))
      - SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion_robot
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
  WHERE REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')
    AND IFNULL(cantidad_pendiente, 0) > 0
  GROUP BY maquina
)
""".strip()


def deterministic_planner_candidate_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Find robots with historical evidence of producing the same article."""
    normalized = normalized_business_text(question)
    if not re.search(
        r"\b(?:podria|puede|conviene)\b.*\b(?:asumir|recibir|absorber)\b|"
        r"\b(?:robot|equipo|maquina)\s+alternativ[oa]\b|\b(?:reasignar|repartir|trasladar|mover|pasar)\b.*\b(?:articulo|pieza|referencia|carga|trabajo|pedido|of|pev)\b|"
        r"\b(?:otros?|otras?)\s+(?:robots?|maquinas?|equipos?)\b.*\b(?:hacer|fabricar|producir|asumir)\b|"
        r"\b(?:donde|en que)\b.*\b(?:hacer|fabricar|producir|mover)\b.*\b(?:articulo|pieza|referencia)\b|"
        r"\b(?:compara|comparar|experiencia|carga|oee)\b.*\bcandidatos?\b|"
        r"\bcandidatos?\b.*\b(?:compara|comparar|experiencia|carga|oee)\b",
        normalized,
    ):
        return None
    filters = active_filters(question, history)
    article = filters.get("articulo")
    source_machine = filters.get("maquina")
    if not article or not source_machine:
        return None
    article_sql = article.replace("'", "''")

    return f"""
WITH
plan_origen AS (
  SELECT
    UPPER(TRIM(articulo)) AS articulo,
    UPPER(TRIM(maquina)) AS maquina_origen,
    SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente_origen,
    SUM(IFNULL(horas_totales_aplicadas, 0)) AS horas_aplicadas_origen,
    SUM(IFNULL(horas_desviacion, 0)) AS horas_desviacion_origen,
    SUM(IFNULL(turnos_aplicados, 0))
      - SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion_origen,
    MIN(fecha_necesidad) AS primera_fecha_necesidad
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
  WHERE UPPER(TRIM(articulo)) = '{article_sql}'
    AND UPPER(TRIM(maquina)) = '{source_machine}'
    AND IFNULL(cantidad_pendiente, 0) > 0
  GROUP BY articulo, maquina_origen
),
{planner_historical_oee_ctes()}
SELECT
  origen.articulo,
  origen.maquina_origen,
  origen.cantidad_pendiente_origen,
  origen.horas_aplicadas_origen,
  origen.horas_desviacion_origen,
  origen.turnos_desviacion_origen,
  origen.primera_fecha_necesidad,
  hist_origen.oee_pct AS oee_historico_origen,
  carga_origen.cantidad_pendiente_robot AS cantidad_pendiente_robot_origen,
  carga_origen.horas_desviacion_robot AS horas_desviacion_robot_origen,
  carga_origen.turnos_desviacion_robot AS turnos_desviacion_robot_origen,
  hist_candidato.maquina AS maquina_candidata,
  hist_candidato.calidad_pct AS calidad_historica_candidato,
  hist_candidato.disponibilidad_pct AS disponibilidad_historica_candidato,
  hist_candidato.rendimiento_pct AS rendimiento_historico_candidato,
  hist_candidato.oee_pct AS oee_historico_candidato,
  hist_candidato.total_piezas_producidas AS experiencia_piezas_candidato,
  carga.cantidad_pendiente_robot AS cantidad_pendiente_candidato,
  carga.horas_aplicadas_robot AS horas_aplicadas_candidato,
  carga.horas_desviacion_robot AS horas_desviacion_candidato,
  carga.turnos_desviacion_robot AS turnos_desviacion_candidato,
  CASE
    WHEN carga.maquina IS NULL THEN 'COMPATIBLE_SIN_CARGA_REGISTRADA'
    WHEN carga.horas_desviacion_robot <= carga_origen.horas_desviacion_robot
      AND hist_candidato.oee_pct >= hist_origen.oee_pct THEN 'FAVORABLE_EN_DATOS'
    WHEN carga.horas_desviacion_robot > carga_origen.horas_desviacion_robot
      AND hist_candidato.oee_pct < hist_origen.oee_pct THEN 'DESFAVORABLE_EN_DATOS'
    ELSE 'MIXTO'
  END AS evaluacion_candidato
FROM plan_origen AS origen
JOIN historico AS hist_origen
  ON hist_origen.articulo = origen.articulo
 AND hist_origen.maquina = origen.maquina_origen
JOIN historico AS hist_candidato
 ON hist_candidato.articulo = origen.articulo
 AND hist_candidato.maquina != origen.maquina_origen
LEFT JOIN carga_actual AS carga_origen
  ON carga_origen.maquina = origen.maquina_origen
LEFT JOIN carga_actual AS carga
  ON carga.maquina = hist_candidato.maquina
ORDER BY hist_candidato.total_piezas_producidas DESC, hist_candidato.oee_pct DESC
LIMIT 10
""".strip()


def deterministic_balance_suggestions_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Read redistribution decisions exclusively from vw_sugerencias_balanceo."""
    normalized = lexical_intent_text(question)
    if not re.search(
        r"\b(?:redistribu|reasigna|repart|traslad|mover|pasar|equilibr|balance|"
        r"destino|alternativ|asumir|absorber|liberar|sugerenc|propuest)\w*\b",
        normalized,
    ):
        return None
    if not re.search(
        r"\b(?:carga|trabajo|produccion|pedido|orden|of|pev|pieza|articulo|referencia|"
        r"robot|maquina|equipo|destino|sugerencia|propuesta|turnos?)\w*\b",
        normalized,
    ):
        return None

    # Merge scope dimension by dimension: a new robot replaces the previous
    # robot without discarding an active week from the conversation. When the
    # origin changes, do not inherit the previous origin's article.
    current_filters = active_filters(question, [])
    filters = active_filters(question, history)
    if current_filters.get("maquina") and not current_filters.get("articulo"):
        filters.pop("articulo", None)
        # A detailed previous answer may contain the movement date and hide the
        # broader week stated by the user. On an origin switch, recover that
        # user-supplied week and discard the previous movement's exact date.
        if not current_filters.get("semana") and not current_filters.get("fecha_desde"):
            inherited_weeks: list[int] = []
            for turn in reversed(history):
                inherited_weeks = planner_requested_weeks(str(turn.get("user", "")))
                if inherited_weeks:
                    break
            if inherited_weeks:
                year = int(filters.get("anio") or datetime.now(MADRID_TZ).year)
                filters["semana"] = f"{year}-{int(inherited_weeks[0]):02d}"
                filters.pop("fecha_desde", None)
                filters.pop("fecha_hasta", None)
    predicates = ["piezas_a_mover > 0", "turnos_destino_nuevos > 0"]
    if filters.get("maquina"):
        predicates.append(f"UPPER(TRIM(maquina_origen)) = '{filters['maquina']}'")
    if filters.get("articulo"):
        article = filters["articulo"].replace("'", "''")
        predicates.append(f"UPPER(TRIM(articulo)) = '{article}'")
    if filters.get("semana"):
        _, week = filters["semana"].split("-", 1)
        # In the balance suggestions view, ``semana`` is the ISO week number
        # and ``fecha`` identifies its ISO year. A user-supplied year is kept
        # separately by active_filters(); an implicit current year is not.
        if filters.get("anio"):
            predicates.append(
                f"EXTRACT(ISOYEAR FROM fecha) = {int(filters['anio'])}"
            )
        predicates.append(f"SAFE_CAST(semana AS INT64) = {int(week)}")
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")

    where_clause = " AND\n  ".join(predicates)
    if re.search(
        r"\b(?:cuantas?|numero|total)\b.{0,20}\b(?:sugerencias?|propuestas?|movimientos?)\b",
        normalized,
    ):
        return f"""
SELECT COUNT(*) AS numero_sugerencias
FROM `{PROJECT_ID}.{DATASET_ID}.vw_sugerencias_balanceo`
WHERE {where_clause}
""".strip()
    return f"""
SELECT
  fecha,
  pev,
  articulo,
  semana,
  maquina,
  maquina_origen,
  accion_sugerida,
  piezas_a_mover,
  turnos_origen_liberados,
  destino_recomendado,
  turnos_destino_nuevos,
  otras_opciones_compatibles
FROM `{PROJECT_ID}.{DATASET_ID}.vw_sugerencias_balanceo`
WHERE {where_clause}
ORDER BY
  fecha,
  turnos_origen_liberados DESC,
  piezas_a_mover DESC,
  maquina_origen,
  destino_recomendado
LIMIT 10
""".strip()


def deterministic_planner_redistribution_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Rank overloaded plan lines that have an evidence-backed robot candidate."""
    normalized = normalized_business_text(question)
    if not re.search(r"\b(?:redistribu|reasigna|repart|traslad|mover|pasar|equilibr|balance)\w*\b", normalized):
        return None
    if not re.search(r"\b(?:carga|cargas|trabajo|backlog|cartera|plan|planes|produccion|pedidos?|ordenes?|ofs?|pevs?)\b", normalized):
        return None

    return f"""
WITH
plan_lineas AS (
  SELECT
    pev,
    UPPER(TRIM(articulo)) AS articulo,
    UPPER(TRIM(maquina)) AS maquina_origen,
    SUM(IFNULL(cantidad_pendiente, 0)) AS cantidad_pendiente_origen,
    SUM(IFNULL(horas_totales_aplicadas, 0)) AS horas_aplicadas_origen,
    SUM(IFNULL(horas_desviacion, 0)) AS horas_desviacion_origen,
    SUM(IFNULL(turnos_aplicados, 0))
      - SUM(IFNULL(turnos_teoricos_erp, 0)) AS turnos_desviacion_origen,
    MIN(fecha_necesidad) AS fecha_necesidad,
    MIN(semana_necesidad) AS semana_necesidad
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_planificador_capacidad`
  WHERE REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')
    AND IFNULL(cantidad_pendiente, 0) > 0
  GROUP BY pev, articulo, maquina_origen
),
{planner_historical_oee_ctes()},
candidatos AS (
  SELECT
    plan.pev,
    plan.articulo,
    plan.maquina_origen,
    plan.cantidad_pendiente_origen,
    plan.horas_aplicadas_origen,
    plan.horas_desviacion_origen,
    plan.turnos_desviacion_origen,
    plan.fecha_necesidad,
    plan.semana_necesidad,
    hist_origen.oee_pct AS oee_historico_origen,
    carga_origen.cantidad_pendiente_robot AS cantidad_pendiente_robot_origen,
    carga_origen.horas_desviacion_robot AS horas_desviacion_robot_origen,
    carga_origen.turnos_desviacion_robot AS turnos_desviacion_robot_origen,
    hist_candidato.maquina AS maquina_candidata,
    hist_candidato.calidad_pct AS calidad_historica_candidato,
    hist_candidato.disponibilidad_pct AS disponibilidad_historica_candidato,
    hist_candidato.rendimiento_pct AS rendimiento_historico_candidato,
    hist_candidato.oee_pct AS oee_historico_candidato,
    hist_candidato.total_piezas_producidas AS experiencia_piezas_candidato,
    carga.cantidad_pendiente_robot AS cantidad_pendiente_candidato,
    carga.horas_aplicadas_robot AS horas_aplicadas_candidato,
    carga.horas_desviacion_robot AS horas_desviacion_candidato,
    carga.turnos_desviacion_robot AS turnos_desviacion_candidato,
    CASE
      WHEN carga.maquina IS NULL THEN 'COMPATIBLE_SIN_CARGA_REGISTRADA'
      WHEN carga.horas_desviacion_robot <= carga_origen.horas_desviacion_robot
        AND hist_candidato.oee_pct >= hist_origen.oee_pct THEN 'FAVORABLE_EN_DATOS'
      WHEN carga.horas_desviacion_robot > carga_origen.horas_desviacion_robot
        AND hist_candidato.oee_pct < hist_origen.oee_pct THEN 'DESFAVORABLE_EN_DATOS'
      ELSE 'MIXTO'
    END AS evaluacion_candidato
  FROM plan_lineas AS plan
  JOIN historico AS hist_origen
    ON hist_origen.articulo = plan.articulo
   AND hist_origen.maquina = plan.maquina_origen
  JOIN historico AS hist_candidato
   ON hist_candidato.articulo = plan.articulo
   AND hist_candidato.maquina != plan.maquina_origen
  LEFT JOIN carga_actual AS carga_origen
    ON carga_origen.maquina = plan.maquina_origen
  LEFT JOIN carga_actual AS carga
    ON carga.maquina = hist_candidato.maquina
  WHERE plan.horas_desviacion_origen > 0
)
SELECT
  pev,
  articulo,
  maquina_origen,
  cantidad_pendiente_origen,
  horas_aplicadas_origen,
  horas_desviacion_origen,
  turnos_desviacion_origen,
  fecha_necesidad,
  semana_necesidad,
  oee_historico_origen,
  cantidad_pendiente_robot_origen,
  horas_desviacion_robot_origen,
  turnos_desviacion_robot_origen,
  maquina_candidata,
  calidad_historica_candidato,
  disponibilidad_historica_candidato,
  rendimiento_historico_candidato,
  oee_historico_candidato,
  experiencia_piezas_candidato,
  cantidad_pendiente_candidato,
  horas_aplicadas_candidato,
  horas_desviacion_candidato,
  turnos_desviacion_candidato,
  evaluacion_candidato
FROM candidatos
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY pev, articulo, maquina_origen
  ORDER BY
    CASE evaluacion_candidato
      WHEN 'FAVORABLE_EN_DATOS' THEN 1
      WHEN 'MIXTO' THEN 2
      WHEN 'COMPATIBLE_SIN_CARGA_REGISTRADA' THEN 3
      ELSE 4
    END,
    experiencia_piezas_candidato DESC,
    oee_historico_candidato DESC
) = 1
ORDER BY
  CASE evaluacion_candidato
    WHEN 'FAVORABLE_EN_DATOS' THEN 1
    WHEN 'MIXTO' THEN 2
    WHEN 'COMPATIBLE_SIN_CARGA_REGISTRADA' THEN 3
    ELSE 4
  END,
  fecha_necesidad,
  horas_desviacion_origen DESC
LIMIT 10
""".strip()


def business_sql_error(question: str, history: list[dict[str, str]], sql: str) -> str | None:
    """Detect high-impact semantic mistakes before BigQuery executes valid SQL."""
    normalized_sql = re.sub(r"\s+", " ", sql.lower())
    normalized_question = question.lower()

    filters = active_filters(question, history)
    # The planning view uses ``semana_necesidad`` rather than the OEE view's
    # ``semana`` column.  Keep the strict business-week check for historical
    # OEE/parada queries, but never reject a valid planner query merely because
    # it does not contain ``semana = ...``.
    is_capacity_planner_sql = "vw_planificador_capacidad" in normalized_sql
    is_balance_sql = "vw_sugerencias_balanceo" in normalized_sql
    is_planner_sql = is_capacity_planner_sql or is_balance_sql
    if (
        filters.get("semana")
        and filters.get("modo_consulta") != "planificador"
        and not is_planner_sql
    ):
        week = re.escape(filters["semana"].lower())
        exact_week_filter = re.search(
            rf"(?:\b\w+\.)?\bsemana\b\s*=\s*['\"]{week}['\"]",
            normalized_sql,
        )
        if not exact_week_filter:
            return (
                f"El periodo debe filtrarse mediante semana = '{filters['semana']}'. "
                "No conviertas una semana de negocio en un intervalo sobre fecha."
            )

    if filters.get("fecha_desde") and not is_planner_sql:
        start = filters["fecha_desde"].lower()
        end = filters.get("fecha_hasta", start).lower()
        uses_oee = "vw_oee_master" in normalized_sql
        uses_stops = "vw_import_paradas" in normalized_sql
        expected_column_present = (
            uses_oee and bool(re.search(r"\bfecha\b", normalized_sql))
        ) or (
            uses_stops and "fecha_operativa" in normalized_sql
        )
        if not expected_column_present or start not in normalized_sql or end not in normalized_sql:
            column = "fecha_operativa" if uses_stops and not uses_oee else "fecha"
            return (
                f"El intervalo temporal ya está resuelto y debe aplicarse exactamente sobre {column}: "
                f"{start} a {end}. No vuelvas a interpretar ni sustituyas esas fechas."
            )

    if (
        filters.get("modo_consulta") == "planificador"
        and planner_requested_weeks(question)
        and is_capacity_planner_sql
        and "semana_necesidad" not in normalized_sql
    ):
        return (
            "Las semanas del planificador deben filtrarse con semana_necesidad, no con la "
            "columna semana de la vista histórica de OEE."
        )

    if (
        filters.get("modo_consulta") == "planificador"
        and planner_requested_weeks(question)
        and is_balance_sql
        and not re.search(r"\bsemana\b", normalized_sql)
    ):
        return "Las sugerencias de balanceo deben filtrarse mediante la columna semana."

    if is_high_volume_oee_opportunity(question):
        required_aliases = ("total_piezas_producidas", "perdida_oee_eur")
        missing_aliases = [alias for alias in required_aliases if alias not in normalized_sql]
        if missing_aliases:
            return (
                "La selección por alto volumen debe devolver para cada artículo los alias exactos "
                "total_piezas_producidas y perdida_oee_eur. El volumen debe formar parte del "
                "resultado porque justifica el criterio solicitado."
            )

    if (
        ("perdida_oee_eur" in normalized_sql or "perdida_rendimiento_eur" in normalized_sql)
        and "h_teoricas" not in normalized_sql
    ):
        return (
            "La fórmula de pérdida es incorrecta: debe usar "
            "SUM(tiempo_erp) - SUM(h_teoricas), nunca piezas_teo en esa resta."
        )

    asks_longest_stops = bool(re.search(
        r"\b(?:paradas?|incidencias?|aver[ií]as?|tiempos? muertos?|detenciones?|interrupciones?)\b"
        r".*\b(?:m[aá]s\s+larg[oa]s|mayor(?:es)?\s+duraci[oó]n)\b",
        normalized_question,
    ))
    consolidates_with_row_number = (
        "row_number" in normalized_sql
        and re.search(r"partition\s+by\s+(?:\w+\.)?id_parada", normalized_sql)
    )
    consolidates_segments = (
        re.search(r"group\s+by\s+(?:\w+\.)?id_parada", normalized_sql)
        and re.search(r"sum\s*\(\s*(?:ifnull\s*\(\s*)?(?:\w+\.)?tiempo_parada_min", normalized_sql)
    )
    if asks_longest_stops and not (consolidates_with_row_number or consolidates_segments):
        return (
            "El ranking de incidencias debe consolidar los tramos antes del LIMIT mediante "
            "GROUP BY id_parada y SUM(tiempo_parada_min), o deduplicar filas idénticas de "
            "forma equivalente."
        )
    return None


def deterministic_machine_metric_ranking_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Rank all RB machines deterministically by the explicitly requested KPI."""
    normalized = normalized_business_text(question)
    if not re.search(r"\b(?:mejor|peor|mayor|menor)\b", normalized):
        return None
    if not re.search(r"\b(?:maquina|maquinas|robot|robots|equipo|equipos)\b", normalized):
        return None
    metric_alias = None
    if re.search(r"\brendimiento\b", normalized):
        metric_alias = "rendimiento_pct"
    elif re.search(r"\bdisponibilidad\b", normalized):
        metric_alias = "disponibilidad_pct"
    elif re.search(r"\bcalidad\b", normalized):
        metric_alias = "calidad_pct"
    elif re.search(r"\b(?:oee|ooe)\b", normalized):
        metric_alias = "oee_pct"
    if metric_alias is None:
        return None

    filters = active_filters(question, history)
    predicates = ["REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')"]
    if filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
    else:
        return None
    where_clause = " AND\n    ".join(predicates)
    ascending = bool(re.search(r"\b(?:peor|menor)\b", normalized))
    direction = "ASC" if ascending else "DESC"
    offset = 1 if re.search(r"\b(?:segund[oa])\b", normalized) else 0
    return f"""
WITH agregados AS (
  SELECT
    UPPER(TRIM(maquina)) AS maquina,
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
  WHERE {where_clause}
  GROUP BY maquina
), indicadores AS (
  SELECT
    maquina,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct
  FROM agregados
  WHERE total_piezas_producidas > 0
    AND tiempo_plan > 0
    AND piezas_teo > 0
), resultados AS (
  SELECT
    maquina,
    calidad_pct,
    disponibilidad_pct,
    rendimiento_pct,
    calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct
  FROM indicadores
)
SELECT
  maquina,
  {metric_alias}
FROM resultados
WHERE {metric_alias} IS NOT NULL
ORDER BY {metric_alias} {direction}, maquina
LIMIT 1 OFFSET {offset}
""".strip()


def deterministic_open_oee_diagnostic_sql(
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    """Provide a broad but bounded data set for open operational questions."""
    normalized = normalized_business_text(question)
    if not re.search(
        r"\b(?:preocupa|preocupar|preocupante|diagnostico|diagnosticar|analiza|analisis|"
        r"prioridad|priorizar|atencion|vigilar|destaca|destacan|problemas?)\b",
        normalized,
    ):
        return None
    mode = query_mode(question, [])
    if mode in {"planificador", "paradas"}:
        return None
    filters = active_filters(question, history)
    predicates: list[str] = []
    if filters.get("semana"):
        predicates.append(f"semana = '{filters['semana']}'")
    elif filters.get("fecha_desde"):
        predicates.append(
            f"fecha BETWEEN DATE '{filters['fecha_desde']}' "
            f"AND DATE '{filters['fecha_hasta']}'"
        )
    elif filters.get("anio"):
        predicates.append(f"EXTRACT(YEAR FROM fecha) = {int(filters['anio'])}")
    else:
        return None
    if re.search(r"\b(?:robots?|equipos?)\b", normalized):
        predicates.append("REGEXP_CONTAINS(UPPER(TRIM(maquina)), r'^RB[0-9]+$')")
    where_clause = " AND\n    ".join(predicates)
    return f"""
WITH agregados AS (
  SELECT
    UPPER(TRIM(maquina)) AS maquina,
    SUM(IFNULL(buenas, 0)) AS buenas,
    SUM(IFNULL(reoperar, 0)) AS reoperar,
    SUM(IFNULL(buenas, 0) + IFNULL(reoperar, 0)) AS total_piezas_producidas,
    SUM(IFNULL(tiempo_plan, 0)) AS tiempo_plan,
    SUM(IFNULL(tiempo_erp_capado, 0)) AS tiempo_erp_capado,
    SUM(IFNULL(tiempo_erp, 0)) AS tiempo_erp,
    SUM(IFNULL(h_teoricas, 0)) AS h_teoricas,
    SUM(IFNULL(piezas_teo, 0)) AS piezas_teo
  FROM `{PROJECT_ID}.{DATASET_ID}.vw_oee_master`
  WHERE {where_clause}
  GROUP BY maquina
), indicadores AS (
  SELECT
    maquina,
    buenas,
    reoperar,
    total_piezas_producidas,
    tiempo_plan,
    tiempo_erp_capado,
    tiempo_erp,
    h_teoricas,
    piezas_teo,
    LEAST(SAFE_DIVIDE(buenas, total_piezas_producidas), 1) AS calidad_pct,
    SAFE_DIVIDE(tiempo_erp_capado, tiempo_plan) AS disponibilidad_pct,
    LEAST(SAFE_DIVIDE(total_piezas_producidas, piezas_teo), 1) AS rendimiento_pct,
    tiempo_plan - tiempo_erp_capado AS horas_inactivas,
    ((tiempo_plan - tiempo_erp_capado)
      + (tiempo_erp - h_teoricas)
      + IFNULL(SAFE_DIVIDE(reoperar, total_piezas_producidas) * tiempo_erp, 0))
      * 24.9 AS perdida_oee_eur
  FROM agregados
  WHERE total_piezas_producidas > 0
)
SELECT
  maquina,
  total_piezas_producidas,
  reoperar AS piezas_reoperar,
  calidad_pct,
  disponibilidad_pct,
  rendimiento_pct,
  calidad_pct * disponibilidad_pct * rendimiento_pct AS oee_pct,
  horas_inactivas,
  perdida_oee_eur
FROM indicadores
WHERE calidad_pct IS NOT NULL
  AND disponibilidad_pct IS NOT NULL
  AND rendimiento_pct IS NOT NULL
ORDER BY perdida_oee_eur DESC, maquina
LIMIT 10
""".strip()


def semantic_rewrite_question(
    question: str,
    schema: str,
    history: list[dict[str, str]],
    feedback: str | None = None,
) -> str:
    """Translate free plant language into a clear analytical request.

    The result remains natural language on purpose: it can preserve open-ended
    analysis and multi-part requests that would be lost in a rigid intent JSON.
    SQL safety is still enforced later by the existing validators.
    """
    normalized_original = normalized_business_text(question)
    deterministic_temporal = resolve_temporal_context(question)
    if deterministic_temporal.get("fecha_desde"):
        deterministic_period_instruction = (
            "Periodo resuelto de forma determinista y obligatorio: "
            f"{deterministic_temporal['fecha_desde']} a "
            f"{deterministic_temporal['fecha_hasta']}. No lo recalcules ni lo cambies."
        )
    else:
        deterministic_period_instruction = "No hay un periodo determinista adicional."
    has_explicit_scope = bool(
        deterministic_temporal.get("fecha_desde")
        or re.search(r"\b20\d{2}\s*[-/]\s*\d{1,2}\b", question)
        or re.search(r"\b(?:semana|s)\s*[-_/]?\s*\d{1,2}\b", question, re.IGNORECASE)
        or re.search(r"\b20\d{2}\b", question)
    )
    explicit_entity = bool(
        re.search(r"\bRB\s*[-_ ]?\s*\d+\b|\b(?:robot|m[aá]quina|equipo)\s*\d+\b", question, re.IGNORECASE)
        or re.search(
            r"\b(?:art[ií]culo|pieza|referencia|producto)\s+[A-Z0-9][A-Z0-9.\-]{7,}\b",
            question,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:empiez\w*|comienz\w*|prefijo|c[oó]digo)\D{0,25}\d{3,6}\b", question, re.IGNORECASE)
    )
    explicit_scope_reset = bool(re.search(
        r"\b(?:todos|todas)\s+(?:los|las)\s+(?:robots?|m[aá]quinas?|equipos?|art[ií]culos?|referencias?|piezas?)\b|"
        r"\b(?:global(?:es)?|de\s+planta|todo\s+el\s+historico)\b",
        normalized_original,
    ))
    period_only_update = bool(
        has_explicit_scope
        and not explicit_entity
        and not explicit_scope_reset
        and _conversation_domain(question) is None
    )
    needs_previous_context = bool(re.search(
        r"\b(?:ese|esa|eso|esos|esas|mismo|misma|anterior|antes|"
        r"comparalo|comparala|compáralo|compárala|dame\s+detalles|por\s+que|por\s+qué)\b",
        normalized_original,
    ))
    broad_standalone_scope = bool(
        not needs_previous_context
        and re.search(
            r"\b(?:robots|equipos|maquinas|referencias|articulos|piezas)\b",
            normalized_original,
        )
        and re.search(
            r"\b(?:sobrecargad|saturad|mas\s+cantidad|mayor\s+cantidad|"
            r"carga\s+pendiente|segun\s+el\s+plan|ranking|global)\w*\b",
            normalized_original,
        )
    )
    # A self-contained request with its own period starts a fresh analytical
    # scope. This prevents an old article or machine from leaking into a new
    # question such as «qué equipos van más apretados la semana que entra».
    recent_history = (
        [] if (
            (has_explicit_scope and not needs_previous_context and not period_only_update)
            or broad_standalone_scope
        )
        else history[-4:] if history
        else []
    )
    if active_filters(question, history).get("requiere_aclaracion"):
        return question
    ranking_across_machines = bool(
        not re.search(r"\bRB\s*[-_ ]?\s*\d+\b", question, re.IGNORECASE)
        and re.search(r"\b(?:mejor|peor|mayor|menor)\b", normalized_original)
        and re.search(r"\b(?:maquina|maquinas|robot|robots|equipo|equipos)\b", normalized_original)
    )
    if ranking_across_machines and history:
        inherited = active_filters("", history)
        if inherited.get("semana"):
            temporal_context = f"Periodo anterior: semana {inherited['semana']}."
        elif inherited.get("fecha_desde"):
            temporal_context = (
                f"Periodo anterior: {inherited['fecha_desde']} a "
                f"{inherited.get('fecha_hasta', inherited['fecha_desde'])}."
            )
        elif inherited.get("anio"):
            temporal_context = f"Periodo anterior: año {inherited['anio']}."
        else:
            temporal_context = ""
        recent_history = [{"user": temporal_context, "assistant": ""}] if temporal_context else []
    retry_text = (
        f"\nEl intento anterior no pudo resolverse. Motivo: {feedback}\n"
        if feedback
        else ""
    )
    prompt = f"""
Actúa como intérprete de lenguaje de operarios para un asistente industrial de OEE.
Reformula el mensaje del usuario como una petición analítica clara en español que otro
modelo pueda convertir a GoogleSQL. Devuelve únicamente la petición reformulada, sin
explicaciones, títulos, Markdown, JSON ni SQL.

Principios:
- Conserva toda la intención del usuario, incluidas preguntas abiertas, comparaciones,
  explicaciones, rankings, simulaciones y solicitudes con varias métricas.
- Usa el historial para resolver pronombres y elipsis, pero el mensaje actual tiene prioridad.
- Traduce vocabulario coloquial de planta: robot/equipo/línea a máquina; pieza/referencia/código
  a artículo; piezas malas/retrabajo/a reoperar a reoperar; paros/tiempos muertos a paradas;
  carga/cartera/OF/PEV a planificación; repartir/mover carga a sugerencias de balanceo.
- Normaliza máquinas como RB6, RB8, etc., semanas como 2026-38 y fechas como 2026-09-10.
- La fecha actual en Europe/Madrid es {datetime.now(MADRID_TZ).date().isoformat()}.
- {deterministic_period_instruction}
- Interpreta formatos humanos como 10/septiembre, 10 septiembre, 10 del 9, ayer, semana
  pasada, semana que viene, agosto y último trimestre. No inventes una fecha si no hay
  ninguna referencia temporal expresa ni contexto inequívoco.
- Si el usuario menciona un código numérico corto como 220 al hablar de artículos, conserva
  que se trata de todos los artículos cuyo código empieza por 220.
- No inventes máquinas, artículos, métricas, umbrales ni periodos.
- En rankings como «peor máquina» o «mejor robot», hereda el periodo anterior pero compara
  todas las máquinas RB; no conviertas la máquina de la consulta anterior en un filtro.
- Conserva literalmente la métrica solicitada: rendimiento sigue siendo rendimiento y OEE
  sigue siendo OEE. No sustituyas una por otra.
- No respondas la pregunta. Solo hazla inequívoca y consultable.
- Si ya es clara, devuélvela prácticamente igual.
{retry_text}
Vistas y columnas autorizadas:
{schema}

Historial reciente:
{json.dumps(recent_history, ensure_ascii=False)}

Mensaje original:
{question}
"""
    try:
        response = ai.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.1,
                thinking_config=ThinkingConfig(thinking_budget=0),
                max_output_tokens=512,
            ),
        )
        rewritten = (response.text or "").strip().strip("`").strip()
        if not rewritten or len(rewritten) > 4000:
            return question
        start = deterministic_temporal.get("fecha_desde")
        end = deterministic_temporal.get("fecha_hasta")
        if start and start == end and start not in rewritten:
            rewritten += f" Fecha exacta obligatoria: {start}."
        elif start and end and (start not in rewritten or end not in rewritten):
            rewritten += f" Periodo exacto obligatorio: desde {start} hasta {end}."
        return _reconcile_period_only_kpi_follow_up(question, rewritten, history)
    except Exception:
        logging.exception("No se pudo interpretar semánticamente la pregunta")
        return question


def make_sql(question: str, schema: str, history: list[dict[str, str]]) -> str:
    filters = active_filters(question, history)
    if filters.get("requiere_aclaracion") == "maquinas":
        alternatives = filters.get("alternativas_maquina", "").split(",")
        if len(alternatives) == 2:
            return (
                f"ACLARAR: ¿Quieres analizar {alternatives[0]} o {alternatives[1]}, "
                "o compararlos?"
            )
    deterministic_builders = (
        deterministic_machine_metric_ranking_sql,
        deterministic_open_oee_diagnostic_sql,
        deterministic_balance_suggestions_sql,
        deterministic_planner_article_load_sql,
        deterministic_planner_week_load_sql,
        deterministic_availability_breakdown_sql,
        deterministic_stop_summary_sql,
        deterministic_top_stops_sql,
        deterministic_stop_details_sql,
        deterministic_oee_target_savings_sql,
        deterministic_machine_metric_difference_sql,
        deterministic_single_kpi_sql,
    )
    for builder in deterministic_builders:
        deterministic_sql = builder(question, history)
        if deterministic_sql:
            logging.info("Constructor determinista seleccionado: %s", builder.__name__)
            return deterministic_sql
    history_text = json.dumps(history, ensure_ascii=False) if history else "[]"
    filters_text = json.dumps(active_filters(question, history), ensure_ascii=False)
    prompt = f"""
Eres un analista industrial experto en OEE. Convierte la pregunta en UNA consulta GoogleSQL de solo lectura.

Contexto de negocio:
- OEE significa Eficiencia Global de los Equipos. Interpreta también errores comunes como OOE u OEE.
- El usuario no necesita conocer los nombres técnicos de las vistas. Interpreta el vocabulario
  habitual de planta con estas equivalencias:
  * cartera, backlog, pedidos pendientes, órdenes pendientes, OF, PEV, cola de producción o
    trabajo pendiente = planificación y carga pendiente;
  * ocupación, saturación, sobrecarga, desvío o desviación = carga y desviaciones del planificador;
  * repartir, equilibrar, balancear, mover, trasladar o reasignar trabajo = redistribución;
  * pieza, referencia o producto = artículo;
  * robot, máquina o equipo = máquina;
  * avería, tiempo muerto, detención o interrupción = incidencia/parada;
  * fecha de entrega, vencimiento o compromiso = fecha_necesidad cuando se hable de pedidos.
  * léxico flexible: OEE/eficiencia global, rendimiento/velocidad/productividad/ritmo,
    paradas/paros/tiempos muertos/downtime, artículo/referencia/SKU/código de pieza,
    máquina/robot/equipo/línea/célula y OF/orden de fabricación/PEV/pedido son equivalentes.
  * tolera abreviaturas y errores habituales como RB 8, robot 8, s38, semana 38 u OOE.
- Para indicadores, evolución y comparaciones de OEE usa preferentemente vw_oee_master.
- Para incidencias, causas, motivos y duración de paradas usa preferentemente vw_import_paradas.
 - Para carga de trabajo, planificación y órdenes pendientes usa vw_planificador_capacidad.
   Para redistribución, balanceo, reasignación o destinos recomendados usa exclusivamente
   vw_sugerencias_balanceo: la propuesta ya está calculada y no debes recalcular candidatos.
 - Si la pregunta menciona dos o más robots explícitamente, compáralos todos mediante un filtro
   `maquina IN (...)` o una condición equivalente; no conserves solo el primer robot detectado.
- En vw_import_paradas, `oee = 'SI'` significa parada OEE, no planificada o incidencia;
  `oee = 'NO'` significa parada planificada o No OEE. Para "paradas OEE", "incidencias" o
  "paradas no planificadas" filtra exactamente `UPPER(TRIM(oee)) = 'SI'`. Para "No OEE" o
  "paradas planificadas" filtra exactamente `UPPER(TRIM(oee)) = 'NO'`. Nunca busques los
  literales `OEE`, `NO OEE`, `TRUE` o `FALSE` en esa columna.
- Las fechas de vw_import_paradas ya usan turno operativo con corte a las 06:00. No vuelvas a ajustar esas fechas.
- vw_oee_master ya integra y prorratea las paradas. No vuelvas a restarlas, sumarlas o prorratearlas al calcular indicadores.
- No unas las dos vistas salvo que la pregunta lo requiera y existan claves inequívocas en el esquema.
- Una semana comienza el lunes. Para semanas usa DATE_TRUNC(fecha, WEEK(MONDAY)) cuando sea compatible con el tipo de campo.
- No asumas que la última fecha cargada coincide con hoy: si preguntan por el último dato disponible, usa MAX sobre la fecha apropiada.
- Usa el historial para resolver referencias como "esa semana", "la misma máquina", "peor rendimiento" o respuestas breves a una aclaración.
- El mensaje actual tiene prioridad sobre el historial. No arrastres filtros antiguos cuando el usuario indique otros nuevos.
- Resuelve pronombres y elipsis ("eso", "ese robot", "la anterior", "dame detalles", "¿por qué?")
  usando la última pregunta y respuesta relevante. Si el usuario corrige una métrica, fecha o
  entidad, la corrección sustituye al contexto anterior.
- Para tendencias compara una serie homogénea (por ejemplo, meses con meses), indica si el último
  periodo es parcial y no confundas un dato diario con un promedio mensual. Si una respuesta
  anterior contradice la serie calculada, reconócelo y corrígelo explícitamente.
- Explica el razonamiento con las cifras consultadas: separa hechos calculados de interpretación,
  no inventes causas y no respondas "no puedo consultar" cuando la respuesta esté en el historial.
- CONTEXTO ACTIVO contiene filtros recuperados de los mensajes del usuario. En preguntas de
  continuación que no indiquen una máquina o semana nuevas, aplica obligatoriamente esos filtros.
- No elimines un filtro activo solo porque la pregunta actual sea breve. Solo sustitúyelo cuando
  el mensaje actual proporcione expresamente otro valor.
- Si CONTEXTO ACTIVO contiene fecha_desde y fecha_hasta, son fechas ya resueltas de forma
  determinista y debes aplicarlas obligatoriamente. En vw_oee_master filtra `fecha` y en
  vw_import_paradas filtra `fecha_operativa`, ambos inclusive. No vuelvas a interpretar la
  expresión relativa original ni pidas al usuario una fecha que ya aparece resuelta.
- «Último trimestre» significa el último trimestre natural cerrado. Usa siempre el intervalo
  explícito que figure en CONTEXTO ACTIVO y muéstralo también en la explicación de cálculo.
- Si CONTEXTO ACTIVO no contiene `fecha_desde`, `semana` ni `anio`, queda prohibido inventar
  una fecha concreta, escoger una fila diaria o introducir por iniciativa propia un filtro sobre
  fecha. Si contiene `alcance_temporal = historico`, agrega todo el histórico disponible después
  de aplicar los filtros de máquina y artículo, sin elegir un día representativo.
- En continuaciones hipotéticas como «si subimos el OEE al 65 %», conserva exactamente el
  artículo, máquina, periodo y nivel de agregación de la respuesta anterior. No cambies un
  agregado histórico por una fila diaria.
- Una estimación de ahorro por objetivo de OEE debe declarar la hipótesis. Si no se especifica
  qué componente mejora, usa únicamente el escenario proporcional: porcentaje de brecha cerrada
  = (OEE objetivo - OEE actual) / (1 - OEE actual); ahorro estimado = pérdida OEE actual por ese
  porcentaje. No presentes esta estimación como un ahorro garantizado.
- Si modo_consulta es `paradas`, responde desde vw_import_paradas. Si es `global`, agrega todas
  las filas filtradas sin agrupar por artículo. Si es `por_articulo`, agrupa por artículo.
 - Si modo_consulta es `planificador` y no se pide redistribución, responde desde vw_planificador_capacidad. Usa `cantidad_pendiente`
   como carga pendiente, `horas_totales_aplicadas` y `turnos_aplicados` como carga asignada,
   `horas_desviacion` y `turnos_desviacion` como desviación, y `fecha_necesidad`/`semana_necesidad`
   como fecha de entrega. No confundas `oee_aplicado` con el OEE calculado desde vw_oee_master.
 - Si el usuario pregunta qué robot tiene "más carga pendiente", responde con `cantidad_pendiente`
   en unidades y ordénala de mayor a menor; no respondas con `turnos_desviacion` ni presentes la
   desviación como si fuera la carga. Si pregunta por desviación de horas o turnos, responde con
   la métrica de desviación y sus unidades correspondientes.
- Calcula siempre la desviación agregada de turnos como
  `SUM(IFNULL(turnos_aplicados, 0)) - SUM(IFNULL(turnos_teoricos_erp, 0))`. No uses AVG ni
  sumes porcentajes para obtenerla.
- En el planificador, una petición de `semana 38`, `semanas 38 y 39` o expresiones equivalentes
  filtra `semana_necesidad`; no filtres la columna `semana` de vw_oee_master. Si se indica un año,
  aplícalo mediante `EXTRACT(YEAR FROM fecha_necesidad)`.
 - En redistribución consulta directamente vw_sugerencias_balanceo y devuelve fecha, PEV, artículo,
   semana, máquina de origen, acción sugerida, piezas a mover, turnos liberados, destino recomendado,
   turnos nuevos del destino y otras opciones compatibles. No reconstruyas esta decisión desde
   vw_planificador_capacidad, vw_oee_master ni otras vistas.
 - El plan semanal tiene una capacidad máxima de 15 turnos por robot. En las sugerencias de
   balanceo, `turnos_origen_liberados` es la carga que se elimina del origen y
   `turnos_destino_nuevos` son los turnos ajustados por OEE que necesitará el destino para fabricar
   las piezas trasladadas. Si el destino requiere más turnos de los que se liberan, explica que la
   propuesta resuelve o reduce la sobrecarga del origen, pero consume más capacidad productiva
   total; no la presentes como una mejora de eficiencia. Si requiere menos, puede mejorar también
   la eficiencia esperada. La vista limita la asignación a la capacidad semanal disponible.
- Las recomendaciones son informativas y de solo lectura. Nunca generes INSERT, UPDATE, DELETE,
  MERGE ni afirmes que se ha modificado un plan.
- Si modo_consulta es `global_y_por_articulo`, devuelve en una sola consulta el agregado global
  y todos los artículos del periodo mediante UNION ALL o GROUPING SETS. Añade una columna
  `nivel` con `GLOBAL` o `ARTICULO`, y calcula cada KPI por separado a partir de los SUM de cada
  nivel. No limites el desglose a dos artículos salvo que solamente existan dos.

{KPI_BUSINESS_RULES}

{GLOBAL_QUERY_RULES}

Reglas obligatorias:
- Usa exclusivamente las vistas incluidas en el esquema.
- Escribe todos los nombres de vista completos entre acentos graves.
- Devuelve solo SQL, sin explicación ni Markdown.
- Nunca uses SELECT *.
- Limita el resultado a {MAX_RESULT_ROWS} filas como máximo.
- Para fechas relativas, usa CURRENT_DATE('Europe/Madrid').
- No inventes columnas. Si la pregunta no puede resolverse con el esquema, devuelve exactamente: NO_SE_PUEDE
- Si falta un dato imprescindible de la pregunta (por ejemplo máquina o periodo) y no hay una interpretación razonable, devuelve: ACLARAR: seguido de una sola pregunta breve.
- Conserva nombres descriptivos para las columnas calculadas mediante alias en español sin espacios.
- Si agrupas por máquina, artículo, turno, fecha o semana, calcula las fórmulas dentro de cada grupo usando SUM; no calcules primero el porcentaje por fila.
- Para una semana escrita como 2026-22, filtra preferentemente la columna semana por ese valor exacto.
- Interpreta `semana 33` y `s33` como la semana 33 del año contenido en CONTEXTO ACTIVO;
  si no existe otro año explícito, usa el año actual de Europe/Madrid. Interpreta `robot 8`,
  `robot RB8`, `máquina 8` y `RB8` como la máquina `RB8`.

ESQUEMA:
{schema}

HISTORIAL RECIENTE DE ESTA CONVERSACIÓN:
{history_text}

CONTEXTO ACTIVO DETERMINISTA:
{filters_text}

PREGUNTA:
{question}
"""
    response = ai.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0,
            thinking_config=ThinkingConfig(thinking_budget=0),
            max_output_tokens=2048,
        ),
    )
    return extract_sql(response.text or "")


def repair_sql(question: str, schema: str, invalid_sql: str, error: str) -> str:
    """Ask the model to repair one BigQuery compilation error."""
    prompt = f"""
Corrige la consulta GoogleSQL indicada usando exclusivamente el esquema autorizado.

Reglas obligatorias:
- Devuelve solo la consulta corregida, sin explicación ni Markdown.
- Conserva la intención, filtros, periodo, agrupación y orden solicitados.
- No inventes columnas ni selecciones alias que no hayan sido creados.
- Solo SELECT o WITH; ninguna escritura.
- Usa nombres de vista completos entre acentos graves.
- Máximo {MAX_RESULT_ROWS} filas.

Pregunta original:
{question}

Esquema autorizado:
{schema}

Consulta rechazada:
{invalid_sql}

Error de BigQuery:
{error}
"""
    response = ai.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0,
            thinking_config=ThinkingConfig(thinking_budget=0),
            max_output_tokens=2048,
        ),
    )
    return extract_sql(response.text or "")


def json_value(value):
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


def execute_query(sql: str) -> list[dict]:
    dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_job = bq.query(sql, job_config=dry_config, location=BIGQUERY_LOCATION)
    if (dry_job.total_bytes_processed or 0) > MAXIMUM_BYTES_BILLED:
        raise ValueError("La consulta supera el límite de datos permitido.")

    config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAXIMUM_BYTES_BILLED,
        use_query_cache=True,
        labels={"application": "chatbot-aad"},
    )
    rows = bq.query(sql, job_config=config, location=BIGQUERY_LOCATION).result(
        max_results=MAX_RESULT_ROWS
    )
    result = [
        {key: json_value(value) for key, value in dict(row).items()}
        for row in rows
    ]
    for row in result:
        for key, value in list(row.items()):
            if key.lower() == "oee" and isinstance(value, str):
                normalized = value.strip().upper()
                if normalized == "SI":
                    row["significado_oee"] = "OEE / no planificada / incidencia"
                elif normalized == "NO":
                    row["significado_oee"] = "No OEE / planificada"
    return result


def explain(question: str, rows: list[dict], history: list[dict[str, str]], sql: str) -> str:
    if not rows:
        if "vw_import_paradas" in sql:
            filters = active_filters(question, history)
            scope = []
            if filters.get("maquina"):
                scope.append(f"la máquina {filters['maquina']}")
            if filters.get("fecha_desde"):
                scope.append(
                    f"el periodo {filters['fecha_desde']}–{filters['fecha_hasta']}"
                )
            elif filters.get("semana"):
                scope.append(f"la semana {filters['semana']}")
            scope_text = " para " + " y ".join(scope) if scope else " con los filtros aplicados"
            return (
                f"No se encontraron registros de paradas{scope_text}; por tanto, "
                "no hay incidencias de este resultado que permitan asociar una causa al periodo."
            )
        if re.search(
            r"\b(?:redistribu|reasigna|mover|trasladar|repartir|balancear|equilibrar|"
            r"convendr[ií]a|podr[ií]a|asumir|robot\s+alternativo)\w*\b",
            question,
            re.IGNORECASE,
        ):
            return (
                "No hay sugerencias de balanceo calculadas para esos filtros en la vista actual."
            )
        return "No encontré resultados para esa consulta."
    prompt = f"""
Eres el asistente interno de OEE de planta. Responde en español usando únicamente los resultados adjuntos.

Reglas:
- Empieza con la respuesta directa.
- Si hay un único periodo y una única máquina, responde normalmente en una sola frase y no añadas viñetas que repitan los mismos valores.
- Añade viñetas solo cuando aporten comparaciones, anomalías o información diferente de la frase principal.
- Incluye periodo, máquina, artículo o turno cuando estén disponibles.
- Los campos OEE o pct_* entre 0 y 1 son proporciones: muéstralos como porcentaje con dos decimales.
- Mantén las duraciones en la unidad indicada por el nombre o los datos; no inventes unidades.
- Distingue OEE de No OEE y no atribuyas causas que no estén en los resultados.
- Al relacionar incidencias con un KPI, distingue una coincidencia temporal de una causa
  demostrada. Una parada del periodo no prueba por sí sola que causara el valor del indicador.
- En datos de paradas, el valor SI de la columna oee se redacta como "OEE / no planificada" y
  el valor NO como "No OEE / planificada". No inviertas esta equivalencia.
- Equivalencia inmutable: SI = no planificada; NO = planificada. Por ejemplo, si los resultados
  indican SI: 4 y NO: 2, responde 4 no planificadas y 2 planificadas, nunca al revés.
- La consulta SQL adjunta ya ha sido validada y ejecutada. Si contiene un filtro
  `UPPER(TRIM(oee)) = 'SI'`, todas sus filas son paradas OEE/no planificadas aunque la columna oee
  no aparezca en el SELECT. No digas que falta la clasificación. El equivalente con 'NO' son
  paradas planificadas.
- Si el resultado es parcial, vacío o ambiguo, dilo claramente.
- En una pregunta de ranking (mejor, peor, segunda, tercera, top), una sola fila después de
  ordenar y limitar es una respuesta completa. No digas que faltan otras filas ni que "solo se
  dispone" de esa máquina.
- Si el usuario solicita N elementos concretos (por ejemplo, cinco paradas) y el número de filas
  devueltas es menor que N, no introduzcas la lista como "los/las N". Indica explícitamente que
  solo se encontraron X elementos usando la cifra numérica (por ejemplo, "4 paradas") y enumera
  esos X resultados. El número de filas adjunto es la
  referencia exacta para esta comprobación.
- Si el usuario pregunta por la segunda peor o segunda mejor y el resultado contiene una máquina,
  responde directamente que esa máquina ocupa la segunda posición solicitada.
- En una continuación de ranking, no añadas métricas que el usuario no haya pedido en ese mensaje.
  Si los resultados contienen columnas auxiliares, utilízalas para verificar el orden pero omítelas
  de la redacción final.
- Los campos terminados en `_puntos_porcentuales` ya están expresados en puntos sobre una escala
  de 0 a 100: no vuelvas a multiplicarlos ni los presentes como porcentaje relativo.
- Una diferencia entre dos proporciones, por ejemplo 0.6518 y 0.3022, son 34.96 puntos
  porcentuales. Nunca la redactes como 0.35 puntos porcentuales.
- No menciones SQL, vistas, tablas ni detalles técnicos.
- Para listados de incidencias individuales incluye `id_parada` y las horas de inicio y fin cuando
  estén disponibles. No presentes dos filas como si fueran duplicadas solo porque compartan motivo,
  fecha y duración: identifica cada una por su id.
- `fecha_operativa` es el día de negocio y puede ser distinto de la fecha natural de inicio o fin.
  Para una incidencia individual redacta `inicio_real` y `fin_real` como fechas y horas completas;
  nunca combines `fecha_operativa` con la hora de otro campo ni inventes “del día siguiente”.
- Si aparece `tramos_consolidados`, la fila ya representa una única incidencia consolidada y
  `tiempo_parada_min` es la suma de sus tramos. No la dividas ni la presentes como duplicada.
- No presentes más de 10 elementos salvo cuando el usuario pida expresamente un top de artículos
  desglosado por todas sus máquinas; en ese caso muestra todas las combinaciones devueltas para
  esos artículos, con un máximo de {MAX_RESULT_ROWS} filas.
- Si el usuario solicita un top N de artículos y un desglose por máquina, conserva los N artículos
  aunque el desglose produzca más de N filas. Organiza la respuesta por artículo y debajo por
  máquina. No afirmes que falta la cantidad si existe total_piezas_articulo o
  total_piezas_producidas_maquina en los resultados.
- En rankings por fabricación, indica el total producido que determinó la posición antes de mostrar
  el OEE por máquina. "Fabricado" incluye buenas más reoperar según la fórmula corporativa.
- Si la pregunta usa «alto volumen», «gran volumen» o el volumen de producción como criterio para
  elegir oportunidades de mejora, muestra obligatoriamente las piezas producidas de cada artículo
  listado. No omitas el dato que justifica que el artículo pertenece al grupo de alto volumen.
- Las cantidades de piezas, recuentos de paradas y otros conteos se muestran como números enteros
  sin decimales cuando su valor no tiene parte fraccionaria: escribe 1.888.347 piezas, no
  1.888.347,0 piezas.
- Responde únicamente a lo preguntado. No enumeres campos ausentes ni añadas advertencias sobre componentes que el usuario no solicitó.
- La pregunta actual tiene prioridad sobre el historial: no respondas a una pregunta anterior ni
  arrastres su máquina, artículo o periodo si el mensaje actual los sustituye.
- Toda cifra, fecha, ranking o afirmación sobre producción debe estar respaldada por los resultados
  adjuntos de BigQuery. Si no hay filas o el dato no aparece, dilo claramente y no lo inventes.
- En sugerencias de balanceo, la vista ya ha calculado la propuesta. Explica con claridad la máquina
  de origen, el destino recomendado, las piezas a mover, los turnos liberados en origen y los turnos
  nuevos en destino. Incluye el PEV, artículo y semana cuando estén disponibles.
- Interpreta las sugerencias como balanceo de capacidad semanal: cada robot dispone de 15 turnos.
  Expresa `turnos_destino_nuevos` como «turnos ajustados por OEE necesarios en el destino», no como
  «turnos generados». Cuando el valor sea 15,00, indica que el movimiento ocuparía toda la capacidad
  semanal disponible que la vista ha reservado en ese destino.
- Mantén esa denominación también en respuestas de seguimiento, correcciones y explicaciones del
  impacto: no acortes `turnos_destino_nuevos` a «turnos» si puede confundirse con turnos teóricos.
- Compara los turnos liberados con los necesarios en destino. Si destino necesita más, aclara que
  se alivia la sobrecarga del origen a cambio de consumir más turnos totales; la propuesta es una
  alternativa de capacidad, no una mejora global de eficiencia. No rechaces ni sustituyas por ello
  una sugerencia ya calculada por la vista.
- `otras_opciones_compatibles` son alternativas informativas, no el destino principal. No inventes
  destinos ni cambies el orden de preferencia calculado por la vista.
- La sugerencia sigue siendo de solo lectura: no afirmes que el movimiento se ha ejecutado ni que
  el plan se ha modificado.
- Puedes explicar el funcionamiento del asistente y sus procesos de forma general, pero distingue
  siempre esa explicación técnica de los datos calculados.
- No muestres al usuario el nombre técnico `turnos_aplicados`. Preséntalo siempre como «turnos
  ajustados por OEE». Reserva «turnos teóricos
  ERP» para `turnos_teoricos_erp` y «desviación de turnos» para la diferencia entre ambos.
- Adapta la respuesta al tono de la conversación: habla como un analista que acompaña al usuario,
  no como un formulario. Empieza por la conclusión y añade solo el razonamiento necesario.
- En preguntas de seguimiento como "¿y eso?", "¿por qué?", "¿ha mejorado?" o "dame detalles",
  usa la respuesta anterior y los resultados actuales para continuar el hilo. No exijas que el
  usuario repita filtros que siguen vigentes.
- No califiques valores como altos, bajos, buenos, malos, óptimos o preocupantes salvo que exista un objetivo o umbral oficial en los resultados.
- No añadas interpretaciones cualitativas como “estuvo operativa la mayor parte del tiempo”; limita la respuesta a hechos calculados.
- calidad_pct, disponibilidad_pct, rendimiento_pct, oee_pct y tasa_rechazo_pct se muestran multiplicados por 100, con dos decimales y el símbolo %.
- Los campos *_eur se muestran en euros con dos decimales. El resto de cifras usa como máximo dos decimales salvo que el detalle sea necesario.
- Usa formato numérico español: coma decimal y punto de miles cuando corresponda.
- Un valor negativo de piezas_desviadas significa producción por debajo del teórico; uno positivo significa producción por encima del teórico.
- Aunque el usuario solicite un gráfico, no dibujes barras, tablas ASCII ni bloques de código en
  esta respuesta. Redacta únicamente la explicación textual: Python añadirá después un único
  gráfico determinista construido con los resultados de BigQuery.

Pregunta: {question}
Historial reciente: {json.dumps(history, ensure_ascii=False)}
Contexto activo: {json.dumps(active_filters(question, history), ensure_ascii=False)}
Consulta validada y ejecutada: {sql}
Número de filas devueltas: {len(rows)}
Resultados: {json.dumps(rows, ensure_ascii=False)}
"""
    response = ai.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0.1,
            thinking_config=ThinkingConfig(thinking_budget=0),
            max_output_tokens=2048,
        ),
    )
    return (response.text or "No he podido redactar la respuesta.").strip()


def deterministic_planner_answer(question: str, rows: list[dict]) -> str | None:
    """Answer unambiguous planner extrema without letting the model swap metrics."""
    normalized = normalized_business_text(question)
    if not rows:
        return None
    row = rows[0]
    robot = row.get("maquina") or row.get("equipo")
    if robot is None:
        return None
    week = row.get("semana_necesidad")
    week_text = f" en la semana {week}" if week not in (None, "") else ""
    if re.search(
        r"\b(?:que|cual)\s+(?:robot|equipo|maquina)\s+tiene\s+mas\s+carga\s+pendiente\b",
        normalized,
    ):
        quantity = row.get("cantidad_pendiente")
        if quantity is None:
            return None
        try:
            value_text = f"{float(quantity):,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            value_text = str(quantity)
        return (
            f"El robot con más carga pendiente{week_text} es el {robot}, "
            f"con {value_text} unidades pendientes."
        )
    asks_maximum = bool(re.search(r"\b(?:mayor|mas)\b", normalized))
    asks_hours = bool(re.search(
        r"\b(?:desviacion|desvio)\b.*\bhoras?\b|\bhoras?\b.*\b(?:desviacion|desvio)\b",
        normalized,
    ))
    asks_turns = bool(re.search(
        r"\b(?:desviacion|desvio)\b.*\bturnos?\b|\bturnos?\b.*\b(?:desviacion|desvio)\b",
        normalized,
    ))
    metric_key = "horas_desviacion" if asks_hours else "turnos_desviacion" if asks_turns else None
    if asks_maximum and metric_key and row.get(metric_key) is not None:
        unit = "horas" if asks_hours else "turnos"
        value_text = f"{float(row[metric_key]):.2f}".replace(".", ",")
        return (
            f"El robot con mayor desviación de {unit}{week_text} es el {robot}, "
            f"con {value_text} {unit} de desviación."
        )
    return None


def deterministic_planner_comparison_answer(
    question: str, rows: list[dict], sql: str, history: list[dict[str, str]] | None = None
) -> str | None:
    """Compare two ISO need-weeks, including one absent positive-load result, safely."""
    history = history or []
    if not has_planner_load_comparison_intent(question, history):
        return None
    requested_periods = planner_requested_iso_periods(question)
    if len(requested_periods) != 2 or len(rows) not in (1, 2):
        return None

    # The comparison SQL path filters each ISO year/week pair independently.
    sql_periods = [
        (int(year), int(week))
        for year, week in re.findall(
            r"EXTRACT\(ISOYEAR FROM fecha_necesidad\)\s*=\s*(20\d{2})\s+AND\s+"
            r"CAST\(semana_necesidad AS STRING\)\s*=\s*'?(\d{1,2})'?",
            sql,
            re.IGNORECASE,
        )
    ]
    if (
        "vw_planificador_capacidad" not in sql.lower()
        or not re.search(r"IFNULL\(cantidad_pendiente,\s*0\)\s*>\s*0", sql, re.I)
        or set(sql_periods) != set(requested_periods)
        or len(sql_periods) != 2
    ):
        return None

    expected_machine_matches = re.findall(r"\bRB\s*[-_ ]?\s*(\d+)\b", question, re.I)
    expected_machine = f"RB{expected_machine_matches[0]}" if len(set(expected_machine_matches)) == 1 else None
    filters = active_filters(question, history)
    if filters.get("requiere_aclaracion") or filters.get("maquinas"):
        return None
    if expected_machine is None:
        expected_machine = filters.get("maquina")
    sql_machines = re.findall(
        r"UPPER\(TRIM\(maquina\)\)\s*=\s*'([^']+)'", sql, re.I
    )
    if (
        len(sql_machines) != 1
        or re.search(r"UPPER\(TRIM\(maquina\)\)\s+IN\s*\(", sql, re.I)
        or (expected_machine and sql_machines[0].upper() != expected_machine.upper())
    ):
        return None
    period_rows: dict[tuple[int, int], dict] = {}
    machines = set()
    for row in rows:
        try:
            raw_week = str(row.get("semana_necesidad", "")).strip()
            paired_week = re.fullmatch(r"(20\d{2})[-/](\d{1,2})", raw_week)
            if paired_week:
                row_period = tuple(map(int, paired_week.groups()))
            else:
                week = int(raw_week)
                year_value = row.get("anio_iso")
                if year_value is not None:
                    row_period = (int(year_value), week)
                else:
                    possible_years = [year for year, candidate_week in requested_periods if candidate_week == week]
                    if len(possible_years) != 1:
                        first_date = date.fromisoformat(str(row["primera_fecha_necesidad"]))
                        last_date = date.fromisoformat(str(row["ultima_fecha_necesidad"]))
                        first_iso, last_iso = first_date.isocalendar(), last_date.isocalendar()
                        if (
                            first_iso.week != week or last_iso.week != week
                            or first_iso.year != last_iso.year
                        ):
                            return None
                        row_period = (first_iso.year, week)
                    else:
                        row_period = (possible_years[0], week)
            date.fromisocalendar(*row_period, 1)
            if row_period not in requested_periods or row_period in period_rows:
                return None
            quantity = Decimal(str(row["cantidad_pendiente"]))
            if not quantity.is_finite():
                return None
            machine = str(row.get("maquina") or row.get("equipo") or "").strip().upper()
            if not machine:
                return None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        period_rows[row_period] = row
        machines.add(machine)

    if not set(period_rows).issubset(set(requested_periods)) or len(machines) != 1:
        return None
    machine = next(iter(machines))
    if expected_machine and machine != expected_machine:
        return None
    initial_period, final_period = requested_periods

    def quantity_text(value: Decimal) -> str:
        decimals = 0 if value == value.to_integral_value() else 2
        return spanish_number(value, decimals)

    if len(period_rows) == 1:
        period_details = []
        for period in requested_periods:
            label = f"{period[0]}-{period[1]:02d}"
            row = period_rows.get(period)
            if row is None:
                period_details.append(
                    f"en la semana {label} no se encontraron registros de carga pendiente positiva"
                )
                continue
            try:
                amount = Decimal(str(row["cantidad_pendiente"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return None
            if amount <= 0:
                return None
            period_details.append(
                f"en la semana {label} se encontraron {quantity_text(amount)} unidades de carga pendiente positiva"
            )
        return (
            f"Para {machine}, la consulta encontró: " + "; ".join(period_details) + ". "
            "Al faltar una fila para uno de los periodos, no se puede calcular la diferencia "
            "ni la variación porcentual."
        )

    try:
        initial = Decimal(str(period_rows[initial_period]["cantidad_pendiente"]))
        final = Decimal(str(period_rows[final_period]["cantidad_pendiente"]))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    difference = final - initial

    start_label = f"{initial_period[0]}-{initial_period[1]:02d}"
    end_label = f"{final_period[0]}-{final_period[1]:02d}"
    if difference > 0:
        direction = "aumentó"
    elif difference < 0:
        direction = "disminuyó"
    else:
        direction = "se mantuvo sin variación"
    comparison = (
        f"La carga pendiente de {machine} {direction}: "
        f"{quantity_text(initial)} unidades en la semana {start_label} y "
        f"{quantity_text(final)} unidades en la semana {end_label}; "
        f"la diferencia absoluta es de {quantity_text(abs(difference))} unidades."
    )
    if initial == 0:
        variation = "El cambio relativo no se puede calcular porque la carga inicial es cero."
    else:
        relative = abs(difference / initial * Decimal(100))
        relative_text = spanish_number(relative, 2)
        if difference > 0:
            relative_description = f"un aumento del {relative_text}%"
        elif difference < 0:
            relative_description = f"una disminución del {relative_text}%"
        else:
            relative_description = f"del {relative_text}%"
        variation = (
            f"La variación relativa fue {relative_description} respecto a la semana inicial."
        )
    return comparison + " " + variation


def deterministic_balance_answer(rows: list[dict]) -> str | None:
    """Render balance counts without asking the model to infer them from context."""
    if not rows or "numero_sugerencias" not in rows[0]:
        return None
    count = int(rows[0].get("numero_sugerencias") or 0)
    noun = "sugerencia de balanceo" if count == 1 else "sugerencias de balanceo"
    return f"Hay {count} {noun} para los filtros indicados."


def deterministic_metric_difference_answer(rows: list[dict]) -> str | None:
    """Render deterministic percentage-point comparisons without another model call."""
    if not rows:
        return None
    row = rows[0]
    required = {
        "maquina_1",
        "maquina_2",
        "metrica",
        "valor_maquina_1_pct",
        "valor_maquina_2_pct",
        "diferencia_puntos_porcentuales",
    }
    if not required.issubset(row):
        return None
    if any(row.get(key) is None for key in required):
        return None
    try:
        value_1 = float(row["valor_maquina_1_pct"]) * 100
        value_2 = float(row["valor_maquina_2_pct"]) * 100
        difference = float(row["diferencia_puntos_porcentuales"])
    except (TypeError, ValueError):
        return None
    metric = str(row["metrica"])
    period = str(row.get("periodo") or "el periodo solicitado")
    return (
        f"La diferencia de {metric} entre {row['maquina_1']} y {row['maquina_2']} "
        f"en {period} es de {difference:.2f} puntos porcentuales "
        f"({row['maquina_1']}: {value_1:.2f}%; {row['maquina_2']}: {value_2:.2f}%)."
    )


def spanish_number(value, decimals: int = 2) -> str:
    """Format a numeric value using Spanish thousands and decimal separators."""
    number = float(value)
    rendered = f"{number:,.{decimals}f}"
    return rendered.replace(",", "X").replace(".", ",").replace("X", ".")


def deterministic_oee_target_savings_answer(rows: list[dict]) -> str | None:
    """Explain a target-OEE scenario without presenting the estimate as guaranteed."""
    if not rows or "ahorro_estimado_eur" not in rows[0]:
        return None
    row = rows[0]
    required = (
        "oee_actual_pct",
        "oee_objetivo_pct",
        "perdida_actual_oee_eur",
        "ahorro_estimado_eur",
        "perdida_restante_estimada_eur",
    )
    if any(row.get(key) is None for key in required):
        return None
    entity_parts = []
    if row.get("articulo"):
        entity_parts.append(f"el artículo {row['articulo']}")
    if row.get("maquina"):
        entity_parts.append(f"la máquina {row['maquina']}")
    entity = " en ".join(entity_parts) if entity_parts else "el conjunto consultado"
    period = str(row.get("periodo") or "todo el histórico disponible")
    actual = float(row["oee_actual_pct"]) * 100
    target = float(row["oee_objetivo_pct"]) * 100
    current_loss = spanish_number(row["perdida_actual_oee_eur"])
    savings = spanish_number(row["ahorro_estimado_eur"])
    remaining = spanish_number(row["perdida_restante_estimada_eur"])
    actual_text = spanish_number(actual)
    target_text = spanish_number(target)
    production = row.get("total_piezas_producidas")
    production_text = (
        f" sobre {spanish_number(production, 0)} piezas producidas" if production is not None else ""
    )
    if target <= actual:
        conclusion = (
            f"El objetivo del {target_text}% no supera el OEE actual del {actual_text}%, "
            "por lo que este escenario no genera ahorro adicional."
        )
    else:
        conclusion = (
            f"Para {entity}, elevar el OEE del {actual_text}% al {target_text}% supondría "
            f"un ahorro estimado de {savings} € en {period}. La pérdida estimada restante "
            f"sería de {remaining} €."
        )
    return conclusion + (
        "\n\n*Cómo se ha calculado:* se ha mantenido el mismo alcance de la conversación "
        f"({period}, {entity}){production_text}. La pérdida de partida es {current_loss} €. "
        "Como no se ha indicado qué componente del OEE mejoraría, se ha aplicado una simulación "
        "proporcional sobre la parte de la brecha hasta el 100 % que cerraría el objetivo. "
        "Es una estimación, no un ahorro garantizado."
    )


def deterministic_high_volume_opportunity_answer(
    question: str,
    rows: list[dict],
) -> str | None:
    """Render volume-based OEE opportunities with the volume justification always visible."""
    if not is_high_volume_oee_opportunity(question):
        return None
    eligible = [
        row for row in rows
        if row.get("articulo") is not None
        and row.get("total_piezas_producidas") is not None
        and row.get("perdida_oee_eur") is not None
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda row: float(row["perdida_oee_eur"]), reverse=True)
    selected = eligible[:5]

    def describe(row: dict) -> str:
        text = (
            f"{spanish_number(row['total_piezas_producidas'], 0)} piezas producidas y "
            f"{spanish_number(row['perdida_oee_eur'])} € de pérdida de OEE"
        )
        if row.get("oee_pct") is not None:
            text += f", con un OEE del {spanish_number(float(row['oee_pct']) * 100)}%"
        return text

    first = selected[0]
    answer = (
        f"El artículo de alto volumen con mayor oportunidad económica de mejora es "
        f"*{first['articulo']}*: {describe(first)}."
    )
    if len(selected) > 1:
        answer += "\n\nOtros artículos con alto potencial de mejora son:\n"
        answer += "\n".join(
            f"- *{row['articulo']}*: {describe(row)}." for row in selected[1:]
        )
    return answer


def append_text_chart(answer: str, question: str, rows: list[dict]) -> str:
    """Append a deterministic monospaced bar chart when the user asks for a graph."""
    normalized = normalized_business_text(question)
    if not rows or not re.search(
        r"\b(?:grafico|grafica|visualiza|representa|dibuja|diagrama|barras?)\w*\b",
        normalized,
    ):
        return answer

    # The model can ignore the formatting instruction and emit an unsafe chart.
    # Remove any such block and rebuild it below with a fixed width.
    if "█" in answer or "░" in answer:
        answer = re.sub(
            r"\n*\*?Gr[aá]fic[oa][^\n]*\*?\s*\n```[\s\S]*?(?:```|\Z)",
            "",
            answer,
            flags=re.IGNORECASE,
        ).rstrip()
        answer = re.sub(
            r"\n*```(?=[\s\S]*[█░])[\s\S]*?(?:```|\Z)",
            "",
            answer,
            flags=re.IGNORECASE,
        ).rstrip()

    metric_specs = [
        (r"\boee\b", ("oee_pct",), "OEE", "%", True),
        (r"\brendimiento\b", ("rendimiento_pct",), "Rendimiento", "%", True),
        (r"\bdisponibilidad\b", ("disponibilidad_pct",), "Disponibilidad", "%", True),
        (r"\bcalidad\b", ("calidad_pct",), "Calidad", "%", True),
        (
            r"\b(?:perdida|coste|euros?)\b",
            ("perdida_oee_eur", "perdida_total_eur", "perdida_eur"),
            "Pérdida OEE",
            "€",
            False,
        ),
        (
            r"\b(?:cantidad|carga)\s+pendiente\b|\bpendientes?\b",
            ("cantidad_pendiente",),
            "Cantidad pendiente",
            "piezas",
            False,
        ),
        (
            r"\bdesviacion\b.*\bturnos?\b|\bturnos?\b.*\bdesviacion\b",
            ("turnos_desviacion",),
            "Desviación de turnos",
            "turnos",
            False,
        ),
        (
            r"\b(?:paradas?|tiempos?\s+muertos?)\b",
            ("tiempo_parada_min", "minutos_parada", "duracion_minutos", "total_minutos"),
            "Tiempo de parada",
            "min",
            False,
        ),
        (
            r"\b(?:produccion|piezas?|volumen)\b",
            ("total_piezas_producidas", "piezas_producidas", "cantidad_producida", "buenas"),
            "Producción",
            "piezas",
            False,
        ),
    ]
    fallback_metrics = [
        (("oee_pct",), "OEE", "%", True),
        (("total_piezas_producidas", "piezas_producidas"), "Producción", "piezas", False),
        (("cantidad_pendiente",), "Cantidad pendiente", "piezas", False),
        (("turnos_desviacion",), "Desviación de turnos", "turnos", False),
        (("perdida_oee_eur",), "Pérdida OEE", "€", False),
        (("tiempo_parada_min", "minutos_parada"), "Tiempo de parada", "min", False),
    ]

    metric_key = None
    metric_title = ""
    unit = ""
    is_percentage = False
    for pattern, candidates, title, candidate_unit, percentage in metric_specs:
        if re.search(pattern, normalized):
            metric_key = next(
                (key for key in candidates if any(row.get(key) is not None for row in rows)),
                None,
            )
            if metric_key:
                metric_title, unit, is_percentage = title, candidate_unit, percentage
                break
    if metric_key is None:
        for candidates, title, candidate_unit, percentage in fallback_metrics:
            metric_key = next(
                (key for key in candidates if any(row.get(key) is not None for row in rows)),
                None,
            )
            if metric_key:
                metric_title, unit, is_percentage = title, candidate_unit, percentage
                break
    if metric_key is None:
        return answer

    temporal_request = bool(re.search(r"\b(?:evolucion|tendencia|serie\s+temporal)\w*\b", normalized))
    temporal_labels = ("periodo", "mes", "fecha", "semana", "semana_necesidad")
    entity_labels = (
        "maquina", "articulo", "tipo_incidencia", "turno", "cliente",
        "origen", "maquina_origen", "destino_recomendado",
    )
    label_candidates = temporal_labels + entity_labels if temporal_request else entity_labels + temporal_labels
    label_key = next(
        (
            key for key in label_candidates
            if len({str(row.get(key)) for row in rows if row.get(key) not in (None, "")}) >= 2
        ),
        None,
    )
    if label_key is None:
        return answer

    points = []
    for row in rows:
        label = row.get(label_key)
        raw_value = row.get(metric_key)
        if label in (None, "") or raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        display_value = value * 100 if is_percentage else value
        points.append((str(label), display_value))
    if len(points) < 2:
        return answer

    if not temporal_request:
        points.sort(key=lambda point: point[1], reverse=True)
    points = points[:15]
    scale_max = 100.0 if is_percentage else max(abs(value) for _, value in points)
    if scale_max <= 0:
        return answer

    bar_width = 20
    label_width = min(max(len(label) for label, _ in points), 22)
    chart_lines = []
    for label, value in points:
        blocks = max(0, min(bar_width, round(abs(value) / scale_max * bar_width)))
        bar = "█" * blocks + "░" * (bar_width - blocks)
        short_label = label if len(label) <= label_width else label[: label_width - 1] + "…"
        decimals = 0 if unit == "piezas" and abs(value) >= 100 else 2
        value_text = spanish_number(value, decimals)
        chart_lines.append(f"{short_label:<{label_width}} | {bar} {value_text} {unit}")

    chart = (
        f"\n\n*Gráfico — {metric_title}*\n"
        "```\n" + "\n".join(chart_lines) + "\n```"
    )
    return answer.rstrip() + chart


def append_calculation_trace(
    answer: str,
    question: str,
    rows: list[dict],
    sql: str,
    history: list[dict[str, str]],
) -> str:
    """Append concise, auditable calculation context to every data-backed answer."""
    if not rows or re.search(r"c[oó]mo se ha calculado", answer, re.IGNORECASE):
        return answer
    filters = active_filters(question, history)
    normalized_sql = re.sub(r"\s+", " ", sql.lower())
    sql_dates = sorted(set(re.findall(r"date\s*'((?:20)\d{2}-\d{2}-\d{2})'", normalized_sql)))
    if "vw_planificador_capacidad" in normalized_sql:
        iso_periods = [
            (int(year), int(week))
            for year, week in re.findall(
                r"extract\(isoyear from fecha_necesidad\)\s*=\s*(20\d{2})\s+and\s+"
                r"cast\(semana_necesidad as string\)\s*=\s*'?(\d{1,2})'?",
                normalized_sql,
            )
        ]
        if iso_periods:
            labels = [f"{year}-{week:02d}" for year, week in iso_periods]
            period = "las semanas de necesidad ISO " + " y ".join(labels)
        else:
            year_match = re.search(r"extract\(year from fecha_necesidad\)\s*=\s*(20\d{2})", normalized_sql)
            weeks_match = re.search(
                r"cast\(semana_necesidad as string\)\s+in\s*\(([^)]*)\)",
                normalized_sql,
            )
            week_numbers = re.findall(r"['\"]?(\d{1,2})['\"]?", weeks_match.group(1)) if weeks_match else []
            if year_match and week_numbers:
                year_label = f" (fecha_necesidad en año calendario {year_match.group(1)})"
                noun = "la semana de necesidad " if len(week_numbers) == 1 else "las semanas de necesidad "
                period = noun + " y ".join(week_numbers) + year_label
            else:
                period = "los periodos de necesidad filtrados por SQL (año no identificable con seguridad)"
    elif len(sql_dates) > 1:
        period = "las fechas " + " y ".join(sql_dates)
    elif filters.get("semana"):
        period = f"la semana {filters['semana']}"
    elif filters.get("fecha_desde"):
        period = (
            filters["fecha_desde"]
            if filters["fecha_desde"] == filters.get("fecha_hasta")
            else f"el periodo {filters['fecha_desde']}–{filters['fecha_hasta']}"
        )
    elif filters.get("anio"):
        period = f"el año {filters['anio']}"
    elif filters.get("alcance_temporal") == "historico":
        period = "todo el histórico disponible"
    else:
        period = "todos los datos disponibles"

    scope = [period]
    if filters.get("articulo_prefijo"):
        scope.append(f"artículos con prefijo {filters['articulo_prefijo']}")
    elif filters.get("articulo"):
        scope.append(f"artículo {filters['articulo']}")
    if filters.get("maquina"):
        scope.append(f"máquina {filters['maquina']}")

    if "vw_sugerencias_balanceo" in normalized_sql:
        method = "se ha leído la sugerencia ya calculada por el plan de balanceo"
    elif "vw_planificador_capacidad" in normalized_sql:
        method = "se han agregado las órdenes pendientes con las métricas de capacidad solicitadas"
    elif "vw_import_paradas" in normalized_sql:
        method = "se han filtrado y consolidado las incidencias antes de contar o sumar su duración"
    else:
        method = (
            "se han agregado primero las filas del alcance indicado y después se han calculado "
            "los indicadores con las fórmulas corporativas del OEE"
        )
    return answer.rstrip() + "\n\n*Cómo se ha calculado:* " + ", ".join(scope) + "; " + method + "."


@app.get("/")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/")
def chat_event():
    event = request.get_json(silent=True) or {}
    workspace_addon = isinstance(event.get("chat"), dict)
    chat_data = event.get("chat", {}) if workspace_addon else event
    if workspace_addon:
        if chat_data.get("addedToSpacePayload"):
            event_type = "ADDED_TO_SPACE"
        elif chat_data.get("removedFromSpacePayload"):
            event_type = "REMOVED_FROM_SPACE"
        elif chat_data.get("messagePayload"):
            event_type = "MESSAGE"
        else:
            event_type = ""
        message = chat_data.get("messagePayload", {}).get("message", {})
    else:
        event_type = chat_data.get("type", "")
        message = chat_data.get("message", {})

    logging.info(
        "Evento recibido: version=%s formato=%s tipo=%s",
        APP_VERSION,
        "workspace_addon" if workspace_addon else "chat_api",
        event_type,
    )

    if event_type == "ADDED_TO_SPACE":
        return chat_response("Hola. Puedo ayudarte a consultar el OEE y las paradas de planta.", workspace_addon)
    if event_type == "REMOVED_FROM_SPACE":
        return ("", 204)

    question = (message.get("argumentText") or message.get("text") or "").strip()
    question = question.strip("`").strip()
    if not question:
        return chat_response("Escribe una pregunta sobre los datos de OEE de planta.", workspace_addon)
    if question.lower().strip(" ¿?¡!.,") in {"hola", "ayuda", "help"}:
        return chat_response(
            (
                "Puedo ayudarte con el OEE y las paradas. Por ejemplo: “¿Cuál es el OEE "
                "de esta semana?”, “Compara el OEE por máquina” o “¿Cuáles son las "
                "principales causas de parada?”"
            ),
            workspace_addon,
        )

    conversation = conversation_id(message)
    if question.lower().strip(" ¿?¡!.,") in {
        "reiniciar memoria",
        "borrar memoria",
        "nueva conversación",
    }:
        if clear_history(conversation):
            try:
                dataset_schema()
            except Exception:
                logging.exception("No se pudo precargar el esquema")
            return chat_response("He borrado el contexto de esta conversación.", workspace_addon)
        return chat_response("No he podido borrar el contexto de esta conversación.", workspace_addon)
    history = load_history(conversation)
    progress_message_name: str | None = None

    def remembered_response(answer: str):
        save_turn(conversation, history, question, answer)
        return final_chat_response(answer, workspace_addon, progress_message_name)

    scope_answer = context_question_answer(question, history)
    if scope_answer:
        return remembered_response(scope_answer)

    if workspace_addon:
        message_payload = chat_data.get("messagePayload", {})
        space_name = (
            (message.get("space") or {}).get("name")
            or (message_payload.get("space") or {}).get("name")
            or ""
        )
        progress_message_name = create_progress_message(space_name)

    if history and is_explanation_request(question) and not is_causal_availability_question(question):
        return remembered_response(explain_history(question, history))

    if is_causal_availability_question(question):
        filters = causal_availability_filters(question, history)
        causal_sql = deterministic_causal_availability_sql(question, history)
        if causal_sql is None:
            if filters.get("maquinas") or filters.get("requiere_aclaracion") == "maquinas":
                alternatives = filters.get("alternativas_maquina") or filters.get("maquinas", "")
                options = " o ".join(part for part in alternatives.split(",") if part)
                return remembered_response(
                    f"¿Qué máquina quieres analizar{': ' + options if options else ''}?"
                )
            missing = []
            if not filters.get("maquina"):
                missing.append("la máquina")
            if not causal_availability_period_label(filters):
                missing.append("el periodo")
            return remembered_response("Para comprobarlo necesito que indiques " + " y ".join(missing or ["un alcance válido"]) + ".")
        try:
            _, causal_allowed_views = dataset_schema()
            oee_sql, stops_sql = causal_sql
            validate_sql(oee_sql, causal_allowed_views)
            validate_sql(stops_sql, causal_allowed_views)
            try:
                oee_rows, oee_error = execute_query(oee_sql), None
            except Exception as error:
                logging.exception("Falló la consulta OEE del diagnóstico causal")
                oee_rows, oee_error = None, type(error).__name__
            try:
                stop_rows, stop_error = execute_query(stops_sql), None
            except Exception as error:
                logging.exception("Falló la consulta de paradas del diagnóstico causal")
                stop_rows, stop_error = None, type(error).__name__
            return remembered_response(deterministic_causal_availability_answer(
                oee_rows,
                stop_rows,
                oee_error,
                stop_error,
                filters.get("maquina"),
                causal_availability_period_label(filters),
            ))
        except Exception as error:
            logging.exception("No se pudo preparar el diagnóstico causal")
            return remembered_response(
                f"No se pudo preparar el diagnóstico causal ({type(error).__name__}); "
                "no hay resultados que permitan concluir si hubo incidencias."
            )

    conversational_answer = capability_response(question, history)
    if conversational_answer:
        return remembered_response(conversational_answer)

    balance_status_answer = balance_application_status_response(question)
    if balance_status_answer:
        return remembered_response(balance_status_answer)

    try:
        schema, allowed_views = dataset_schema()
        analytical_question = semantic_rewrite_question(question, schema, history)
        logging.info(
            "Pregunta interpretada: original=%r normalizada=%r",
            question,
            analytical_question,
        )

        current_temporal = resolve_temporal_context(analytical_question)
        if current_temporal.get("error_fecha"):
            return remembered_response(current_temporal["error_fecha"])
        if current_temporal.get("fecha_futura") == "true":
            return remembered_response(
                f"La fecha indicada ({current_temporal['fecha_desde']}) todavía es futura, "
                "por lo que aún no puede haber datos de producción ni de paradas para ese día."
            )

        # Graph requests for pending capacity are routed from the original wording.
        # This prevents the semantic rewrite from losing the planner intent.
        original_chart_planner_sql = None
        if re.search(
            r"\b(?:grafico|grafica|visualiza|representa|dibuja|diagrama|barras?)\w*\b",
            normalized_business_text(question),
        ):
            original_chart_planner_sql = deterministic_planner_week_load_sql(question, history)
        original_breakdown_sql = deterministic_availability_breakdown_sql(question, history)
        sql = (
            original_chart_planner_sql
            or original_breakdown_sql
            or make_sql(analytical_question, schema, history)
        )
        if original_breakdown_sql:
            logging.info("Desglose de disponibilidad enrutado desde la pregunta original")
        if original_chart_planner_sql:
            logging.info("Consulta gráfica de capacidad enrutada desde la pregunta original")
        if sql == "NO_SE_PUEDE":
            second_question = semantic_rewrite_question(
                question,
                schema,
                history,
                feedback=(
                    "La primera reformulación no se pudo traducir usando las vistas autorizadas. "
                    "Conserva la intención y exprésala con las métricas y dimensiones disponibles."
                ),
            )
            if second_question != analytical_question:
                analytical_question = second_question
                logging.info("Segundo intento semántico: %r", analytical_question)
                sql = make_sql(analytical_question, schema, history)
        logging.info("SQL generado: %s", sql)
        if sql == "NO_SE_PUEDE":
            return remembered_response(
                "No he podido determinar con suficiente seguridad qué análisis necesitas. "
                "Puedo intentarlo si indicas el periodo y, cuando corresponda, la máquina, "
                "el artículo o la métrica que quieres analizar."
            )
        if sql.upper().startswith("ACLARAR:"):
            return remembered_response(sql.split(":", 1)[1].strip())
        validate_sql(sql, allowed_views)
        semantic_error = business_sql_error(analytical_question, history, sql)
        if semantic_error:
            logging.warning("SQL semánticamente inválido; se intentará una reparación: %s", semantic_error)
            sql = repair_sql(analytical_question, schema, sql, semantic_error)
            logging.info("SQL reparado por regla de negocio: %s", sql)
            validate_sql(sql, allowed_views)
            remaining_error = business_sql_error(analytical_question, history, sql)
            if remaining_error:
                raise ValueError(remaining_error)
        try:
            rows = execute_query(sql)
        except BadRequest as error:
            logging.warning("BigQuery rechazó el SQL; se intentará una reparación: %s", error)
            sql = repair_sql(analytical_question, schema, sql, str(error))
            logging.info("SQL reparado: %s", sql)
            validate_sql(sql, allowed_views)
            remaining_error = business_sql_error(analytical_question, history, sql)
            if remaining_error:
                raise ValueError(remaining_error)
            rows = execute_query(sql)
        answer = deterministic_availability_breakdown_answer(question, rows, sql, history)
        if answer is None:
            answer = deterministic_oee_target_savings_answer(rows)
        if answer is None:
            answer = deterministic_high_volume_opportunity_answer(analytical_question, rows)
        if answer is None:
            answer = deterministic_metric_difference_answer(rows)
        if answer is None:
            answer = deterministic_balance_answer(rows)
        if answer is None:
            answer = deterministic_planner_comparison_answer(
                analytical_question, rows, sql, history
            )
        if answer is None:
            answer = deterministic_planner_answer(analytical_question, rows)
        if answer is None:
            answer = explain(analytical_question, rows, history, sql)
        answer = append_text_chart(answer, question, rows)
        answer = append_calculation_trace(answer, analytical_question, rows, sql, history)
        return remembered_response(answer)
    except RuntimeError as error:
        logging.exception("Configuración sin vistas utilizables")
        return final_chat_response(
            f"No hay vistas utilizables con la configuración actual: {error}",
            workspace_addon,
            progress_message_name,
        ), 200
    except Exception:
        logging.exception("Error procesando la pregunta")
        return final_chat_response(
            "He entendido la petición, pero no he podido completar la consulta. "
            "Prueba de nuevo o concreta el periodo y el elemento que quieres analizar.",
            workspace_addon,
            progress_message_name,
        ), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
