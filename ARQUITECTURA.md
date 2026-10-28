# Arquitectura de informes_ia

Describe lo que el código hace hoy (versión 2.2.0). La integración con el server
de TecnoMonitor (pantalla, API, grupos, programaciones, Asana, despliegue) está en
`tecnomonitor-server/docs/25-informes-ia.md`.

## Flujo

```
FUENTE DE DATOS (fuentes.py)                      Asana          manuales PDF
  FuenteServer en producción / FuenteSQL en lab
        │                                            │                │
        ▼                                            ▼                ▼
preprocess.generar_resumen()          asana_client.obtener_tareas_asana()   docs_context
  ├─ event_tracker   (reportes_historicos)        humanos (anonimizados) / automáticos
  ├─ backlog_analyzer (reportes_uso)
  ├─ software_auditor (software_monitoring + últimos backups)
  ├─ control_calidad (avisos sobre los datos)
  └─ comparacion (período anterior, fuentes livianas)
      (grupo.generar_resumen_grupo() para un grupo de hospitales)
        │
        ▼
audiencia.vista_*()               → lo que ve la IA según el público (cliente / interno)
        │
        ▼
ai_report.generar_json_con_ia()   → sólo TEXTOS, con JSON Schema forzado
  (generar_json_grupo() para grupos)
        │
        ▼
ai_report.completar_con_cifras()  → uptime, incidencias, métricas y control de calidad
        │                            los escribe el código (pisan lo que ponga la IA)
        ▼
render (Jinja2 + WeasyPrint) → PDF
```

Todo lo de arriba lo arma `pipeline_real.PipelineReal`. Lo usan los dos modos:

- **Asíncrono** (`historial/servicio.py` + `worker.py`): el que va al dashboard.
  Deja un borrador editable y el PDF sólo sale de un informe aprobado.
- **Sincrónico** (`service.generar_reporte`, CLI `main.py`): genera el PDF en el
  momento, sin borrador. Para probar.

## Fuentes de datos (fuentes.py)

El módulo no consulta tablas: pide los datos a una `FuenteDatos`.

- `FuenteServer` (en el server, `dashboard_app/informes_fuente.py`): lee con la capa `datos/`
  del server, que funciona igual con el SQLite viejo y con el Postgres de producción (métricas
  tipadas + inventario; reconstruye los reportes en bloque). Es la de producción.
- `FuenteSQL` (acá): esquema VIEJO (`reportes_historicos`, `reportes_uso`,
  `software_monitoring`). Para el laboratorio y los tests. **No sirve contra el Postgres de
  producción**: ese esquema no tiene esas tablas.

`PipelineReal(settings, fuente=...)`: sin fuente, usa `DATABASE_URL` / `DB_FILENAME` con
`FuenteSQL`.

## Informes de grupo (grupo.py)

Petición con `alcance: "grupo"` y `grupo: {id, nombre, hospital_ids}` (los resuelve el
server). Consolidado + tabla, una sola llamada a la IA. Fuentes livianas: horas de reporte
(disponibilidad), tabla `alertas` (incidentes), último estado por componente y último backup
(software), KPIs (actividad). Cada fila trae `motivos_atencion` (interno) y
`recomendaciones_cliente` (cliente).

## Público del informe (audiencia.py)

`cliente` y `interno` reciben vistas distintas de los datos: al cliente no le llegan demoras,
colas, borradores, huecos de monitoreo, Mirth, tickets internos ni identificadores técnicos, y
los riesgos del hospital van como recomendaciones. El interno recibe todo y tiene que devolver
`puntos_a_revisar`. El texto para el cliente pasa por un control de vocabulario: los términos
prohibidos generan avisos en `control_calidad` (para el revisor, nunca en el PDF del cliente).
Detalle en el docstring de `audiencia.py` y en docs/25 del server, §7.

## Datos faltantes

Regla: un dato que falta **nunca** se convierte en un valor (0, 100 %, "sin novedades"); se
informa como faltante.

| Caso | Cómo se trata |
|---|---|
| Métrica ausente (ej. CPU de una VM sin lectura) | `None`: no se evalúa, no entra en promedios ni eventos |
| VM "Offline" por error de acceso (`wmi_error`, `ssh_error`) | La VM responde pero no se pudo leer: no es VM apagada, no cuenta para su disponibilidad, se informa en `vms_sin_acceso_reportes` (mismo criterio que el motor de alertas) |
| VM Offline menos de `offline_minutes` | Ruido de lectura: no es evento (el motor recién alerta a los 15 min) |
| RAM de VM con usado == total o total 0 | Lectura imposible (el agente convierte en 0 lo que WMI no devuelve): se trata como sin dato |
| Sin iDRAC (sin energía ni temperatura en todo el período) | `control_calidad` lo avisa; para el cliente está en `datos_no_disponibles` y los campos `energia` / `termica` los deja vacíos el código (la plantilla oculta la sección); en el interno, el código escribe "Sin datos de … iDRAC no configurado o sin respuesta" |
| Sin KPIs, sin software, sin reportes | `control_calidad` (interno) y `datos_no_disponibles` (cliente); la IA tiene prohibido afirmar algo sobre esas áreas |
| Falta una sección en el JSON del borrador | La plantilla la muestra vacía (no corta el PDF) |

**Pendiente en el agente (propuesta para 4.5.6):** `safe_int` / `safe_float` devuelven 0 cuando
WMI no trae un valor. Para CPU (`LoadPercentage`) eso es indistinguible de una VM ociosa; habría
que mandar `null` en esos casos. Lo de la RAM ya se detecta acá.

## Aviso de cambios de estado

`ServicioReportes(..., al_cambiar_estado=f)`: el módulo llama `f(report_id, estado)` al llegar
a `finalizado`, `error` o `aprobado`. El server lo usa para avisar en Asana a las
programaciones. Un fallo del aviso se loguea y no afecta al informe.

## Estados (historial/estados.py)

```
iniciado → en_espera → en_proceso ─┬─> finalizado (borrador) ──> aprobado
                                   ├─> reintentando ──┘ (vuelve a intentar)
                                   └─> error
```

- `finalizado` no es terminal: espera revisión humana (`guardar_json_editado`,
  `aprobar`). Sólo se edita en `finalizado`; `aprobado` es inmutable.
- El PDF se reconstruye bajo demanda desde `data_json` + `grafico_png`
  (no se guarda): no existe PDF de un borrador.
- Al arrancar, el worker marca `error` los informes que quedaron `en_proceso`
  (el worker murió a mitad). Se pueden volver a pedir.
- Claim atómico: `reclamar_siguiente()` pasa `en_espera → en_proceso` con un
  UPDATE condicional; con varios workers sólo uno gana cada informe.

## Reintentos

La preparación (leer la base, pre-procesar, Asana, manuales) corre una sola vez
y no se reintenta: si falla (hospital inexistente, sin datos) el informe queda
en `error`. Sólo se reintenta la llamada a la IA:

| Tipo de fallo | Ejemplos | Intentos totales |
|---|---|---|
| `red` (`FalloRed`) | timeout, API caída, 429 de cuota | 3 |
| `respuesta_invalida` (`RespuestaInvalida`) | texto que no es JSON, faltan claves | 2 |

Cada tipo cuenta sólo sus propios fallos; la columna `intentos` guarda el total.
Si el 429 trae `retryDelay`, se espera ese tiempo; si no, backoff exponencial.

## Validación de la respuesta de la IA (historial/validacion.py)

La API ya fuerza la estructura con `ai_report.ESQUEMA_RESPUESTA`. Además se
valida que estén las claves de primer nivel: `meta` y `resumen` siempre, más
las secciones pedidas en `secciones_a_mostrar`.

## Contrato del JSON del informe

| Clave | Quién la escribe |
|---|---|
| `meta.tipo`, `meta.fecha`, `meta.hospital` | IA (con los valores que le da el prompt) |
| `meta.periodo`, `meta.modelo`, `meta.alcance`, `meta.grupo` | código |
| `resumen.texto`, `infraestructura.*`, `incidencias.analisis`, `calidad.*`, `recomendacion` | IA |
| `puntos_a_revisar` (sólo interno) | IA |
| `situacion_general`, `hospitales_atencion` (sólo grupo) | IA |
| `tabla_hospitales`, `comparacion` | código |
| `resumen.uptime`, `incidencias.externas`, `incidencias.internas` | código |
| `cifras` (métricas, eventos por tipo, disponibilidad por VM, software) | código |
| `control_calidad` (avisos sobre datos faltantes o dudosos) | código |

La plantilla muestra las cifras desde `cifras`, nunca desde texto de la IA.
`control_calidad` se muestra en el informe interno y en la revisión del borrador.

## Almacenamiento

- **Base de TecnoMonitor**: sólo lectura, a través de la fuente de datos (arriba).
- **Base propia** (`historial/almacen.py`, SQLite en `HISTORIAL_DB_FILENAME`):
  tabla `reportes` con estado, petición, `data_json`, `grafico_png`, quién lo
  pidió y quién lo aprobó.
