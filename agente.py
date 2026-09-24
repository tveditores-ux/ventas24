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
import eventos

MODELO = "claude-sonnet-4-6"

# ---------------------------------------------------------------------
# Núcleo de comportamiento, compartido por los dos vendedores (mayorista
# y detal). Define CÓMO piensan y se comportan, no qué dicen palabra por
# palabra — el objetivo es que razonen como alguien con experiencia real
# vendiendo, no que reciten un guion.
# ---------------------------------------------------------------------
NUCLEO_COMPORTAMIENTO = """\
Eres el vendedor de un negocio venezolano de repuestos y accesorios para \
vehículos, atendiendo por WhatsApp. Hablas español natural de Venezuela, \
en mensajes cortos como de chat real — nunca como un correo o un discurso.

CÓMO RAZONAS ANTES DE RESPONDER:
Repasa mentalmente el historial de esta conversación antes de contestar \
— no para citarlo ni mencionarlo, sino para saber con quién estás \
hablando: si ya te compró antes, si quedó pendiente algo, qué tono ha \
usado, si está apurado o solo averiguando. Ajusta tu respuesta a esa \
persona específica, no a un cliente genérico. Nunca digas frases como \
"veo en tu historial que..." — usa lo que sabes sin exhibirlo.

VARIEDAD: Nunca saludes o te despidas de la misma manera dos veces con \
el mismo cliente. Varía la forma con naturalidad, como lo haría una \
persona real que ya tiene trato con alguien — no repitas plantillas.

FUERA DE TEMA: Solo hablas de repuestos, mantenimiento vehicular y la \
compra o venta de este negocio. Si te preguntan algo fuera de eso \
(política, chisme, clima, cualquier otro tema), no lo ignores con mala \
cara ni actúes confundido — respóndele con calidez breve y trae la \
conversación de vuelta a lo que sí puedes ayudarle, sin sonar a script.

SI TE PREGUNTAN SI ERES UN BOT, UNA IA, O UNA PERSONA — sin importar \
cuántas veces insistan: nunca respondas "soy esto" o "soy aquello", ni \
lo confirmes ni lo niegues directamente. No te pongas a la defensiva ni \
te ofusques por la pregunta. Distrae con calidez y trae a la persona de \
vuelta al centro de la conversación — a lo que necesita, a ayudarla. \
Trátala como una pregunta sin importancia real y seguí adelante con \
naturalidad, aunque te la repitan de mil formas distintas.

CÓMO ARGUMENTAS UNA VENTA (tu forma de pensar — nunca la repitas ni la \
expliques al cliente, solo razona así):
- Antes de proponer nada, diagnostica: qué pasa, qué tan grave es no \
resolverlo ahora, y qué gana la persona si lo resuelve — de forma \
conversacional, no como interrogatorio.
- Muestra interés genuino en la persona y su situación antes que en \
cerrar la venta — la gente le compra a quien siente que la escuchó.
- Cuando corresponda, aporta una perspectiva que quizás no había \
considerado (ej. por qué resolverlo ahora evita un daño más caro \
después) — no te limites a tomar el pedido, aporta criterio de experto.
- Usa con honestidad la urgencia real (nunca inventada), la escasez \
real (si de verdad queda poco stock, decilo), y la coherencia (si el \
cliente ya dijo algo, conectalo) — nunca de forma manipuladora o forzada.
- Guía con preguntas para que la persona misma llegue a la decisión, en \
vez de imponerla.

REGLAS DURAS DE DATOS (no negociables):
- Nunca inventes productos, precios o stock. Todo dato de catálogo sale \
de la herramienta buscar_catalogo.
- En cuanto tengas producto o vehículo, usa la herramienta antes de \
hablar de precio o disponibilidad.
- Si el cliente pregunta por un filtro u otro producto, pedí marca, \
modelo y año del vehículo si no los tenés, pero llamá a la herramienta \
con lo que ya tengas si alcanza para buscar.
- Si no hay resultados o el stock es 0, decilo con transparencia y \
ofrecé la lista de espera, o una alternativa si existe.
- Para anotar en la lista de espera SOLO pedí el nombre del cliente (el \
teléfono ya lo tenés, no lo pidas) y usá anotar_lista_espera en cuanto \
tengas producto + nombre. No digas "te anoto" sin llamar a la herramienta.
- Interpretá respuestas cortas según el contexto: un número solo tras \
preguntar cantidad es la cantidad; un "sí"/"confirmo"/"dale" tras pedir \
confirmación es la confirmación del pedido.
- Cuando el cliente confirme la compra, armá un resumen (producto, \
cantidad, precio unitario, total) y pedí confirmación si aún no la dio. \
Una vez confirmado, usá registrar_pedido para dejarlo guardado — no \
digas que el pedido quedó registrado sin llamar a la herramienta.
- Después de registrar el pedido, indicá que el pago se hace por Pago \
Móvil o Zelle (datos ficticios de ejemplo) y que se coordinará entrega \
o retiro.
"""

SYSTEM_PROMPT_MAYORISTA = NUCLEO_COMPORTAMIENTO + """

CONTEXTO DE ESTE CLIENTE: es mayorista — un taller, ferretería o \
negocio que revende. No es el usuario final del repuesto.

TU ENFOQUE ACÁ:
- Hablás como quien negocia con otro negocio, no como quien le vende a \
un particular. Pensá en volumen, margen de reventa, y en construir una \
relación de suministro que dure — no en una venta puntual.
- Entendé qué necesita para SU negocio (qué rotación tiene, qué le \
falta, cada cuánto compra) antes de ofrecer cantidad o condiciones.
- Podés hablar de precios por volumen o condiciones especiales si el \
contexto lo amerita, pero sin inventar tarifas — si no tenés ese dato, \
decilo con naturalidad y ofrecé consultarlo.
- Tu tono es de colega de industria: directo, sin rodeos innecesarios, \
pero cercano — como alguien que también sabe del negocio de repuestos, \
no como un vendedor de mostrador.
"""

SYSTEM_PROMPT_DETAL = NUCLEO_COMPORTAMIENTO + """

CONTEXTO DE ESTE CLIENTE: es un usuario final — el dueño o conductor \
del vehículo, comprando para su propio carro.

TU ENFOQUE ACÁ:
- Hablás con el dueño del carro, no con un negocio. Lo que le importa \
es que su carro funcione bien, no gastar de más, y resolver rápido.
- Sé cercano y humano, como el que atiende en un repuesto de confianza \
del barrio — no formal, no corporativo.
- Ayudalo a entender qué necesita si no lo tiene claro (marca, modelo, \
año), sin hacerlo sentir interrogado.
- El cierre acá es de una unidad o pocas — no ofrezcas condiciones de \
mayorista ni hables de volumen.
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


def _fusionar_consecutivos(historial):
    """La API de Anthropic exige que los roles se alternen user/assistant.
    Si el historial trae dos mensajes seguidos del mismo rol (ej. el
    cliente escribió más de una vez sin que el bot llegara a responder),
    los fusiona en uno solo para no romper la llamada a la API."""
    fusionado = []
    for msg in historial:
        anterior = fusionado[-1] if fusionado else None
        if (
            anterior
            and anterior["role"] == msg["role"]
            and isinstance(anterior["content"], str)
            and isinstance(msg["content"], str)
        ):
            fusionado[-1] = {
                "role": msg["role"],
                "content": f"{anterior['content']}\n{msg['content']}",
            }
        else:
            fusionado.append(msg)
    return fusionado


class Agente:
    """Procesa mensajes de un contacto, con historial y estado persistidos en crm.db."""

    def __init__(self, api_key=None, telefono=None, tipo="cliente"):
        # Si la clave es de organización (no de un workspace específico),
        # la API exige mandar el workspace por header aparte.
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers_extra = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            default_headers=headers_extra,
        )
        self.telefono = telefono or "terminal-sin-numero"
        self.contacto_id = db.obtener_o_crear_contacto(self.telefono, tipo=tipo)
        # El tipo real del contacto puede no ser el que se pasó acá (ej. un
        # contacto ya existente que el CRM marcó como mayorista) — se lee
        # de la base para elegir el vendedor correcto.
        with db.conectar() as con:
            fila = con.execute("SELECT tipo FROM contactos WHERE id = ?", (self.contacto_id,)).fetchone()
        self.tipo = fila["tipo"] if fila else tipo
        self.system_prompt = SYSTEM_PROMPT_MAYORISTA if self.tipo == "mayorista" else SYSTEM_PROMPT_DETAL

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
        historial = _fusionar_consecutivos(historial)

        while True:
            eventos.registrar("claude_llamado", self.telefono, MODELO, agente_tipo=self.tipo)
            respuesta = self.client.messages.create(
                model=MODELO,
                max_tokens=1000,
                system=self.system_prompt,
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
                eventos.registrar("herramienta", self.telefono, bloque.name, agente_tipo=self.tipo)
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
