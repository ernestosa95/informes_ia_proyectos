# Arquitectura de informes_ia

Reemplaza a los "doc 06" y "doc 07" que se citaban en el código y no estaban en
el repo. Describe lo que el código hace hoy (versión 2.1.0).

## Flujo

```
base de TecnoMonitor (Postgres o SQLite)          Asana          manuales PDF
        │                                            │                │
        ▼                                            ▼                ▼
preprocess.generar_resumen()          asana_client.obtener_tareas_asana()   docs_context
  ├─ event_tracker   (reportes_historicos)        humanos (anonimizados) / automáticos
  ├─ backlog_analyzer (reportes_uso)
  ├─ software_auditor (software_monitoring + últimos backups)
  └─ control_calidad (avisos sobre los datos)
        │
        ▼
ai_report.generar_json_con_ia()   → sólo TEXTOS, con JSON Schema forzado
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
| `meta.periodo`, `meta.modelo` | código |
| `resumen.texto`, `infraestructura.*`, `incidencias.analisis`, `calidad.*`, `recomendacion` | IA |
| `resumen.uptime`, `incidencias.externas`, `incidencias.internas` | código |
| `cifras` (métricas, eventos por tipo, disponibilidad por VM, software) | código |
| `control_calidad` (avisos sobre datos faltantes o dudosos) | código |

La plantilla muestra las cifras desde `cifras`, nunca desde texto de la IA.
`control_calidad` se muestra en el informe interno y en la revisión del borrador.

## Almacenamiento

- **Base de TecnoMonitor**: sólo lectura (`conexion.py`). `DATABASE_URL` o el
  archivo SQLite `DB_FILENAME`.
- **Base propia** (`historial/almacen.py`, SQLite en `HISTORIAL_DB_FILENAME`):
  tabla `reportes` con estado, petición, `data_json`, `grafico_png`, quién lo
  pidió y quién lo aprobó.
