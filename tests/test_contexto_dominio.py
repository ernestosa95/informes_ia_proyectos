"""
Contexto del dominio (contexto_dominio.py): apagado por defecto, y para el cliente
sólo lo que puede leer sin jerga técnica ni datos de otros clientes.
"""
from __future__ import annotations

from informes_ia import ai_report, audiencia, contexto_dominio

from .conftest import HOSPITAL
from .test_audiencia import ASANA, NO_PARA_CLIENTE, _resumen

TITULO = "CONTEXTO DEL DOMINIO"


def test_apagado_por_defecto(fuente, monkeypatch):
    monkeypatch.delenv("INFORMES_IA_CONTEXTO_DOMINIO", raising=False)
    for publico in (audiencia.CLIENTE, audiencia.INTERNO):
        prompt = ai_report._construir_prompt(_resumen(fuente), ASANA, "H", publico, HOSPITAL)
        assert TITULO not in prompt


def test_interno_lleva_secciones_1_a_8_sin_lo_no_confirmado():
    texto = contexto_dominio.texto(audiencia.INTERNO)
    for n in range(1, 9):
        assert f"## {n}." in texto, n
    assert "## 9." not in texto and "Preguntas pendientes" not in texto
    assert "(confirmar)" not in texto and "BORRADOR" not in texto
    assert "PROSEPU" in texto            # el bloque interno va, sin las marcas
    assert "<!--" not in texto


def test_cliente_sin_jerga_ni_otros_clientes():
    texto = contexto_dominio.texto(audiencia.CLIENTE)
    assert "## 1." in texto and "## 8." in texto
    assert "## 4." not in texto and "## 7." not in texto
    assert "PROSEPU" not in texto and "OSECAC" not in texto and "<!--" not in texto
    assert not audiencia._REGEX_CLIENTE.search(texto), audiencia._REGEX_CLIENTE.search(texto)


def test_encendido_va_en_los_prompts(fuente, monkeypatch):
    monkeypatch.setenv("INFORMES_IA_CONTEXTO_DOMINIO", "1")
    interno = ai_report._construir_prompt(_resumen(fuente), ASANA, "H", audiencia.INTERNO, HOSPITAL)
    assert TITULO in interno and "APPV" in interno
    cliente = ai_report._construir_prompt(_resumen(fuente), ASANA, "H", audiencia.CLIENTE, HOSPITAL)
    assert TITULO in cliente
    for prohibido in [p for p in NO_PARA_CLIENTE if p not in ("borradores", "backlog")]:
        assert prohibido not in cliente, prohibido
    grupo = {"grupo": {"nombre": "G"}, "hospitales": [], "agregados": {}}
    assert TITULO in ai_report._construir_prompt_grupo(grupo, audiencia.INTERNO)
