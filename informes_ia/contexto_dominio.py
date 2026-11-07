"""
Contexto del dominio (contexto/dominio.md) para el prompt de los informes IA.

Le explica a la IA qué es Suitestensa, qué hace TecnoMonitor y qué significa cada
señal, para que no suponga (ej. llamar "fuga" a la RAM alta de una APPV).

Va APAGADO hasta revisar el archivo: se prende con INFORMES_IA_CONTEXTO_DOMINIO=1.

Qué se manda según el público:
    interno   secciones 1 a 8.
    cliente   sólo SECCIONES_CLIENTE, sin los bloques marcados para el equipo.
Nunca se mandan:
    - el encabezado del archivo ni la sección "Preguntas pendientes";
    - las líneas con "(confirmar)" (inferencias sin verificar);
    - para el cliente, lo encerrado entre <!-- interno --> y <!-- /interno -->.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import audiencia

ARCHIVO = Path(__file__).parent / "contexto" / "dominio.md"

# Para el cliente: quiénes somos y qué le importa. El resto (sufijos de VM, Mirth,
# señales técnicas) le daría a la IA vocabulario que no puede usar con el cliente.
SECCIONES_CLIENTE = {1, 8}
_SECCION_PREGUNTAS = 9

_TITULO_SECCION = re.compile(r"^## (\d+)\.")
_BLOQUE_INTERNO = re.compile(r"<!-- interno -->.*?<!-- /interno -->\n?", re.DOTALL)
_MARCAS_INTERNO = re.compile(r"^<!-- /?interno -->\n", re.MULTILINE)


def activo() -> bool:
    return os.getenv("INFORMES_IA_CONTEXTO_DOMINIO", "0") == "1"


def _secciones(texto: str) -> dict[int, str]:
    """{número: texto de la sección con su título}. Lo anterior a la sección 1 se descarta."""
    secciones: dict[int, list[str]] = {}
    actual: int | None = None
    for linea in texto.splitlines():
        m = _TITULO_SECCION.match(linea)
        if m:
            actual = int(m.group(1))
            secciones[actual] = []
        if actual is not None:
            secciones[actual].append(linea)
    return {n: "\n".join(lineas) for n, lineas in secciones.items()}


def texto(publico: str, archivo: Path = ARCHIVO) -> str:
    """El contexto filtrado para el público (sin el bloque que lo presenta)."""
    contenido = archivo.read_text(encoding="utf-8")
    if publico == audiencia.CLIENTE:
        contenido = _BLOQUE_INTERNO.sub("", contenido)
    else:
        contenido = _MARCAS_INTERNO.sub("", contenido)
    secciones = _secciones(contenido)
    elegidas = [n for n in sorted(secciones)
                if n != _SECCION_PREGUNTAS and (publico != audiencia.CLIENTE or n in SECCIONES_CLIENTE)]
    lineas = [l for n in elegidas for l in secciones[n].splitlines() if "(confirmar)" not in l]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def para_prompt(publico: str) -> str:
    """Bloque listo para el prompt, o "" si el contexto está apagado."""
    if not activo():
        return ""
    return f"""
CONTEXTO DEL DOMINIO (para entender los datos; NO es un dato del período):
- No lo cites ni lo resumas en el informe, y no lo uses como evidencia de lo que pasó.
- Si el contexto y los DATOS DEL PERÍODO no coinciden, valen los datos.

{texto(publico)}
"""
