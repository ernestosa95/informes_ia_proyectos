# informes_ia — Motor de Reportes de Infraestructura

Módulo Python para generar reportes PDF de infraestructura (uptime,
incidentes, térmica, tickets Asana) usando Gemini como motor de redacción.
Pensado para integrarse como librería dentro de **TecnoMonitor** (u otro
backend/dashboard), no para correr como script standalone.

## Qué cambió respecto a la versión anterior

- **Sin credenciales hardcodeadas.** Todo sale de `.env` (ver `.env.example`).
  Las claves que estaban en el código y en `compiler.txt` deben rotarse:
  quedaron expuestas en texto plano.
- **Sin constantes globales.** `TARGET_ID`, `FECHA_INICIO`, `FECHA_FIN` y
  `TIPO_REPORTE` ahora son parámetros de `generar_reporte(...)`.
- **Módulos separados** (`db.py`, `asana_client.py`, `ai_report.py`,
  `render.py`, `docs_context.py`) en vez de un único archivo de 300+ líneas.
- **Errores explícitos** en vez de `except: pass`: `HospitalNoEncontrado`,
  `DatosInsuficientes`, `GeneracionIAFallida`.
- **Plantilla HTML** movida a `templates/reporte.html.j2` (antes era un
  string de Python de 80 líneas).
- **Logging** en vez de `print()`.
- Corregido un bug latente: la espera de `PROCESSING` en la subida de PDFs
  no releía el estado del archivo y podía quedar en loop infinito.

## Instalación

```bash
pip install -r requirements.txt
cp .env.example .env
# completar GEMINI_API_KEY (y ASANA_ACCESS_TOKEN si aplica) en .env
# y DATABASE_URL con la misma URL de Postgres que usa el server
```

### Base de datos

Lee la base de TecnoMonitor en **Postgres** (producción desde el 05/10/2026)
o en un archivo **SQLite** (laboratorio): con `DATABASE_URL` definida usa esa
URL, si no, el archivo `DB_FILENAME`. Mismo criterio que `database.py` del
server. Las diferencias entre motores (JSON como dict o texto, fechas como
datetime o texto) quedan en `informes_ia/conexion.py`.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests                       # SQLite
INFORMES_IA_TEST_PG_URL=postgresql:///base_vacia python -m pytest tests   # + Postgres
```

Los reportes de prueba copian la estructura del agente 4.5
(`tecnomonitor-agent/agent_logic.py`). La base de Postgres de prueba se
vacía y se rellena: usar una base descartable, nunca la de producción.

## Uso por CLI

```bash
python -m informes_ia.main \
    --hospital H09 \
    --desde "2026-01-25 00:00:00" \
    --hasta "2026-02-08 23:59:59" \
    --tipo cliente
```

## Uso como módulo integrable (el caso que te interesa)

Esto es lo que llamarías, por ejemplo, desde un endpoint FastAPI/Flask
detrás del botón "Iniciar Análisis" del dashboard:

```python
from informes_ia import generar_reporte, HospitalNoEncontrado, DatosInsuficientes

try:
    resultado = generar_reporte(
        hospital_id="H09",
        fecha_inicio="2026-01-25 00:00:00",
        fecha_fin="2026-02-08 23:59:59",
        tipo_reporte="cliente",  # o "interno"
    )
except HospitalNoEncontrado:
    ...  # 404 en tu API
except DatosInsuficientes:
    ...  # 422: no hay telemetría en ese rango

print(resultado.ruta_pdf)   # Path al PDF generado
print(resultado.data)       # el JSON que devolvió la IA, por si lo querés
                             # exponer directamente en el dashboard sin
                             # esperar a que el usuario abra el PDF
```

### Ejemplo: endpoint mínimo con FastAPI

```python
from fastapi import FastAPI, HTTPException
from informes_ia import generar_reporte, HospitalNoEncontrado, DatosInsuficientes, GeneracionIAFallida

app = FastAPI()

@app.post("/reportes/{hospital_id}")
def crear_reporte(hospital_id: str, desde: str, hasta: str, tipo: str = "cliente"):
    try:
        r = generar_reporte(hospital_id, desde, hasta, tipo)
    except HospitalNoEncontrado:
        raise HTTPException(404, "Hospital no configurado")
    except DatosInsuficientes:
        raise HTTPException(422, "Sin telemetría en ese rango")
    except GeneracionIAFallida:
        raise HTTPException(502, "Falló la generación con IA, reintentar")
    return {"pdf": str(r.ruta_pdf), "data": r.data}
```

### Re-renderizar sólo el PDF (sin volver a llamar a la IA)

Útil mientras se itera el diseño de `templates/reporte.html.j2`:

```python
from pathlib import Path
from informes_ia import re_renderizar_pdf

re_renderizar_pdf(
    ruta_json=Path("reportes/H09/data_cliente.json"),
    ruta_grafico=Path("reportes/H09/grafico_cliente.png"),
    ruta_pdf=Path("reportes/H09/Reporte_preview.pdf"),
)
```

## Pipeline de pre-procesamiento (4 tablas → resumen compacto → IA)

El objetivo es no pasarle nunca datos crudos a la IA. Con snapshots cada
~5 min, un período de 2 semanas son miles de filas por hospital; en vez de
eso, `preprocess.generar_resumen()` corre 3 analizadores y devuelve un
único dict de ~1-2 KB:

```
reportes_historicos ──► EventTracker       (informes_ia/event_tracker.py)
                          agrupa cruces de umbral consecutivos en EVENTOS
                          con inicio/fin/duración (CPU/RAM/temp/power/RAID/
                          discos/nodos offline), en vez de contar muestras.
                          Usa umbrales de la tabla `configuracion`.

reportes_uso        ──► BacklogAnalyzer    (informes_ia/backlog_analyzer.py)
                          agrega actividad RIS/PACS por modalidad y
                          aproxima backlog de radiología (kpi_rad_*).

software_monitoring ──► SoftwareAuditor    (informes_ia/software_auditor.py)
                          parsea el EAV polimórfico (ssl_certificate /
                          elasticsearch / mirth) según `app_name`.

                          │
                          ▼
                 preprocess.generar_resumen() → dict compacto
                          │
                          ▼
                 ai_report.generar_json_con_ia()  (le pasa el resumen ya
                                                    agregado, no las tablas)
```

Los umbrales (`temp_cpu_max`, `cpu_host_max`, `kpi_rad_threshold_hours`,
`mirth_queued_threshold`, etc.) se leen dinámicamente de la tabla
`configuracion` vía `config_dinamica.get_configuracion()` — nunca están
hardcodeados en el código, así que un cambio en el dashboard de
Configuración se refleja automáticamente sin tocar código.

### Bug corregido durante el diseño

La heurística original marcaba "Fallo Redundancia Energía" cuando
`watts == 0.0 and status == "OK"`. En los datos reales, la fuente
redundante (`PS2`) está en 0W con status `OK` en el 100% de las muestras
de varios hospitales — es el patrón de una fuente en *standby*, no un
fallo. `EventTracker` ahora sólo alerta si el propio `status` reportado
no es `"OK"` (ver TODO más abajo para afinar esto si aparece un status
más granular).

### Correcciones de datos (2026-10-27)

Verificadas contra lo que guardan el agente y el server, con tests en `tests/`:

1. **Uptime**: era el % de filas con `host_status == "online"`, pero el
   server guarda ahí la salud del iDRAC ("OK"...): daba siempre 0 % y la IA
   lo justificaba inventando causas. Ahora es cobertura en el tiempo: % del
   período con el hospital reportando; un hueco de más de `offline_minutes`
   (el umbral de OFFLINE del server) cuenta como caída. Se agregan los huecos
   más largos y la disponibilidad de cada VM.
2. **Energía, RAID, ventiladores y red**: se buscaban fuera de
   `physical_layer` y nunca se evaluaban. Ahora se leen de
   `physical_layer.sensors.power.supplies`, `physical_layer.storage_layer`
   (controladoras, volúmenes y discos, con el mismo criterio que
   `alerts_engine/infra.py`) y `physical_layer.network_health`.
3. **Gráfico de RAM**: `host_ram_usage` está en GB y se dividía por un total
   que no existe en el JSON (16 GB por defecto). Ahora usa
   `physical_layer.telemetry.ram.usage_percent`.
4. **Mirth**: la clave era la instancia y los canales se pisaban. Ahora es
   un registro por canal, con la cola real (`metric_value` = `queued`), la
   cola máxima del período y los errores del período (aumento del contador
   `errored`, contemplando reinicios del canal).
5. **Postgres**: ver "Base de datos".

### TODOs documentados (decisiones tomadas con supuestos, a confirmar)

1. **`elasticsearch.metric_value`** (`software_auditor.py`): no confirmado
   si es un conteo acumulado o de ventana rodante. Hoy se reporta tanto el
   último valor visto como el máximo del período, sin asumir cuál es "el"
   dato correcto.
2. **Umbral de expiración SSL**: no existe una clave en `configuracion`
   para esto. Se usa `ConfiguracionGlobal.ssl_warning_days` (default 30)
   hasta que se defina un valor oficial.
3. **Backlog de radiología por estudio** (`backlog_analyzer.py`): se
   aproxima con el conteo de `borradores` en el último snapshot del
   período, porque `reportes_uso` trae agregados por ventana de 8/24hs, no
   el detalle de cada estudio con su propio timestamp. Si SUITESTENSA
   expone una vista a nivel de estudio individual, migrar este cálculo
   ahí sería mucho más preciso.

## Próximo paso posible

Si en algún momento se define un umbral por hospital (en vez de global
como es hoy `configuracion`), el único cambio necesario es en
`config_dinamica.py`: agregar un parámetro `hospital_id` a `cargar()` y
resolver overrides ahí — el resto del pipeline no se entera.

