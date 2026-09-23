"""
Agente de ventas por WhatsApp para filtros vehiculares y otros artículos.

Usa function calling REAL de la API de Anthropic (no el truco de
navegador del prototipo), así que el modelo no puede inventar
resultados de catálogo: siempre pasa por buscar_catalogo().

El historial de conversación, la lista de espera y los pedidos se
guardan en crm.db (SQLite) por contacto, no en memoria — así sobreviven
a un reinicio del servidor y quedan conectados entre sí.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anthropic import Anthropic
import db

MODELO = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
Eres el agente de ventas por WhatsApp de un negocio venezolano que vende \
filtros para vehículos y otros artículos. Hablas español natural de \
Venezuela, cercano pero profesional, en mensajes cortos como de WhatsApp real.

REGLAS ESTRICTAS:
- NUNCA inventes productos, precios o stock. Todo dato de catálogo debe \
venir de la herramienta buscar_catalogo.
- En cuanto tengas al menos el nombre del producto o la marca del \
vehículo, usa la herramienta antes de responder sobre precio, \
disponibilidad o stock.
- Si el cliente pregunta por un filtro, pide marca, modelo y año del \
vehículo si no los tiene, pero llama a la herramienta con los datos que \
ya tengas si son suficientes para intentar una búsqueda.
- Si no hay resultados o el stock es 0, dilo claramente y ofrece anotarlo \
en la lista de espera, o sugiere una alternativa si existe.
- Para anotar en la lista de espera SOLO pide el nombre del cliente (el \
teléfono ya lo tienes, no lo pidas) y usa la herramienta \
anotar_lista_espera en cuanto tengas producto + nombre. No digas "te anoto" \
sin llamar a la herramienta.
- Interpreta respuestas cortas según el contexto: un número solo tras \
preguntar cantidad es la cantidad; un "sí"/"confirmo"/"dale" tras pedir \
confirmación es la confirmación del pedido.
- Cuando el cliente confirme la compra, arma un resumen (producto, \
cantidad, precio unitario, total) y pide confirmación si aún no la dio. \
Una vez confirmado, usa la herramienta registrar_pedido para dejarlo \
guardado — no digas que el pedido quedó registrado sin llamar a la \
herramienta.
- Después de registrar el pedido, indica que el pago se hace por Pago \
Móvil o Zelle (usa datos ficticios de ejemplo) y que se coordinará \
entrega o retiro.
- Sé breve, como un chat de WhatsApp real.
"""

HERRAMIENTA_CATALOGO = {
    "name": "buscar_catalogo",
    "description": (
        "Busca productos en el catálogo real de filtros y artículos. "
        "Úsala siempre antes de dar precio, disponibilidad o stock."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "marca": {"type": "string", "description": "Marca del vehículo, ej. Toyota"},
            "modelo": {"type": "string", "description": "Modelo del vehículo, ej. Corolla"},
            "anio": {"type": "integer", "description": "Año del vehículo"},
            "nombre": {"type": "string", "description": "Tipo de producto, ej. filtro de aceite"},
        },
    },
}

HERRAMIENTA_LISTA_ESPERA = {
    "name": "anotar_lista_espera",
    "description": (
        "Registra al cliente en la lista de espera de un producto sin stock, "
        "para avisarle cuando se reponga. El teléfono se agrega automáticamente, "
        "no lo pidas."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre_cliente": {"type": "string", "description": "Nombre del cliente"},
            "producto": {"type": "string", "description": "Descripción del producto que espera, ej. 'Filtro de aceite FRAM CH10358 - Toyota Corolla'"},
        },
        "required": ["nombre_cliente", "producto"],
    },
}

HERRAMIENTA_PEDIDO = {
    "name": "registrar_pedido",
    "description": (
        "Registra un pedido ya confirmado por el cliente (producto, cantidad y "
        "precio acordado). Úsala solo después de que el cliente confirmó la compra."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "producto": {"type": "string", "description": "Producto vendido, con marca/modelo si aplica"},
            "cantidad": {"type": "integer", "description": "Cantidad de unidades"},
            "precio_unitario": {"type": "number", "description": "Precio por unidad en USD"},
        },
        "required": ["producto", "cantidad", "precio_unitario"],
    },
}


class Agente:
    """Procesa mensajes de un contacto, con historial y estado persistidos en crm.db."""

    def __init__(self, api_key=None, telefono=None, tipo="cliente"):
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.telefono = telefono or "terminal-sin-numero"
        self.contacto_id = db.obtener_o_crear_contacto(self.telefono, tipo=tipo)

    def procesar_mensaje(self, texto_usuario: str, historial_previo=None) -> str:
        """Envía el mensaje del usuario al agente y devuelve la respuesta final en texto.

        Por defecto, el historial de contexto sale de crm.db (lo que ya
        conversó este contacto acá). Si el canal externo (ej. WeCall) ya
        trae su propio historial de mensajes, se puede pasar en
        historial_previo como lista de {"role": "user"|"assistant",
        "content": texto} y se usa eso en su lugar — igual se sigue
        registrando todo en crm.db para el CRM.
        """
        db.tocar_ultimo_contacto(self.contacto_id)

        historial = historial_previo if historial_previo is not None else db.obtener_historial(self.contacto_id)
        historial = list(historial)
        historial.append({"role": "user", "content": texto_usuario})

        while True:
            respuesta = self.client.messages.create(
                model=MODELO,
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                tools=[HERRAMIENTA_CATALOGO, HERRAMIENTA_LISTA_ESPERA, HERRAMIENTA_PEDIDO],
                messages=historial,
            )

            historial.append({"role": "assistant", "content": respuesta.content})
            bloques_herramienta = [b for b in respuesta.content if b.type == "tool_use"]

            if not bloques_herramienta:
                # No hay más herramientas que llamar: esta es la respuesta final.
                texto = "".join(b.text for b in respuesta.content if b.type == "text").strip()
                db.registrar_interaccion(self.contacto_id, "user", texto_usuario)
                db.registrar_interaccion(self.contacto_id, "assistant", texto)
                return texto

            # Ejecutar cada llamada a herramienta (protocolo nativo de
            # Anthropic: esto se mantiene solo en memoria para este turno).
            resultados_herramienta = []
            for bloque in bloques_herramienta:
                params = bloque.input
                resultado = self._ejecutar_herramienta(bloque.name, params)
                resultados_herramienta.append({
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })

            historial.append({"role": "user", "content": resultados_herramienta})

    def _ejecutar_herramienta(self, nombre: str, params: dict):
        if nombre == "buscar_catalogo":
            resultado = db.buscar_catalogo(
                marca=params.get("marca"),
                modelo=params.get("modelo"),
                anio=params.get("anio"),
                nombre=params.get("nombre"),
            )
            print(f"  🔎 buscar_catalogo({params}) → {len(resultado)} resultado(s)")
            return resultado if resultado else {"mensaje": "sin resultados"}

        if nombre == "anotar_lista_espera":
            db.actualizar_contacto(self.contacto_id, nombre=params.get("nombre_cliente"))
            db.registrar_espera(self.contacto_id, params.get("producto"))
            print(f"  📝 anotar_lista_espera({params}) para {self.telefono}")
            return {"mensaje": "Cliente registrado en la lista de espera"}

        if nombre == "registrar_pedido":
            total = db.registrar_pedido(
                self.contacto_id,
                producto=params.get("producto"),
                cantidad=params.get("cantidad", 1),
                precio_unitario=params.get("precio_unitario", 0),
            )
            db.actualizar_contacto(self.contacto_id, estado="cliente")
            print(f"  🧾 registrar_pedido({params}) total=${total} para {self.telefono}")
            return {"mensaje": "Pedido registrado", "total_usd": total}

        return {"mensaje": f"Herramienta desconocida: {nombre}"}
