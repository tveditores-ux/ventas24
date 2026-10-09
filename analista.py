"""
Agente Analista del ERP: responde preguntas del dueño sobre el negocio ("¿qué se vende más?",
"¿qué margen dejamos?", "¿qué se me va a agotar?").

Es un agente SEPARADO de los vendedores: otro prompt, otras herramientas y otro permiso.
- Solo lectura: ninguna herramienta escribe nada.
- Solo datos agregados: no ve teléfonos, nombres ni conversaciones de clientes.
- Solo administración puede consultarlo (lo exige el endpoint).
- Cada pregunta y cada herramienta que usa queda en la tabla agente_acciones.
"""

import json
import os

from anthropic import Anthropic

import agente as base
import db
import erp

MAX_VUELTAS = 6

SISTEMA = """\
Eres el analista del negocio de repuestos de Ventas-24. Respondes en español de Venezuela, claro y directo, \
a la persona que dirige el negocio.

Reglas:
- Usa SOLO los datos que devuelven tus herramientas. Si no tienes un dato, dilo; nunca lo inventes ni lo estimes sin decirlo.
- Los montos están en dólares. "Cobrado" es dinero con pago validado por la administración; "por cobrar" son pedidos \
con existencia confirmada que aún no tienen pago validado.
- El margen solo cuenta las ventas que tienen costo cargado; si la cobertura de costo es baja, adviértelo antes de dar conclusiones.
- Responde corto: primero la respuesta, luego 2 o 3 datos que la respalden. Si ves algo que requiere acción \
(stock bajo, margen que no se puede calcular), dilo en una línea.
- No tienes datos personales de clientes y no los pidas.
"""

HERRAMIENTAS = [
    {
        "name": "reporte",
        "description": "Ventas cobradas, por cobrar, margen bruto, pedidos, productos más vendidos, rotación y estados de los pedidos en los últimos N días.",
        "input_schema": {"type": "object", "properties": {"dias": {"type": "integer", "description": "Ventana en días (1 a 365), por defecto 30"}}},
    },
    {
        "name": "inventario",
        "description": "Resumen del inventario (productos con inventario real, valor a costo, stock bajo, compras pendientes) y los productos bajo su mínimo con la cantidad sugerida a reponer.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _ejecutar(nombre: str, params: dict):
    if nombre == "reporte":
        return erp.reportes(int(params.get("dias") or 30))
    if nombre == "inventario":
        return {"resumen": erp.resumen_inventario(), "reponer": erp.sugerir_reposicion()[:15]}
    return {"error": f"herramienta desconocida: {nombre}"}


def preguntar(pregunta: str, usuario: str = "admin", api_key=None) -> str:
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    cliente = Anthropic(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
    )
    db.registrar_accion("Analista", None, "pregunta", {"usuario": usuario}, pregunta[:500])
    historial = [{"role": "user", "content": pregunta}]
    for _ in range(MAX_VUELTAS):
        r = cliente.messages.create(
            model=base.MODELO, max_tokens=4096, system=SISTEMA, tools=HERRAMIENTAS, messages=historial,
        )
        historial.append({"role": "assistant", "content": r.content})
        usos = [b for b in r.content if b.type == "tool_use"]
        if not usos:
            return "".join(b.text for b in r.content if b.type == "text").strip() or "No pude armar una respuesta."
        resultados = []
        for b in usos:
            salida = _ejecutar(b.name, b.input)
            db.registrar_accion("Analista", None, b.name, b.input, "datos agregados entregados")
            resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(salida, ensure_ascii=False)})
        historial.append({"role": "user", "content": resultados})
    return "La consulta necesitó demasiados pasos; hazla más específica."
