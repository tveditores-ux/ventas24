"""
El vendedor de Mercado Libre: responde preguntas de las publicaciones y
atiende al comprador después de la venta (datos de pago y de envío).

Es un agente aparte del de WhatsApp (agente.py no se toca): Mercado Libre
tiene reglas propias — no se pueden dar teléfonos, WhatsApp, links ni
correos — y el contexto es distinto (una publicación concreta, una orden
concreta).
"""

import json
import os
import re

from anthropic import Anthropic

MODELO = os.environ.get("MELI_MODELO", "claude-opus-5")
NEGOCIO = os.environ.get("MELI_NEGOCIO", "Ventas 24")
DATOS_PAGO = os.environ.get("MELI_DATOS_PAGO", "").replace("\\n", "\n").strip()
OPCIONES_ENVIO = os.environ.get(
    "MELI_OPCIONES_ENVIO",
    "Envío por MRW, Zoom o Tealca (cobro a destino) o retiro personal.",
).replace("\\n", "\n").strip()
HORARIO = os.environ.get("MELI_HORARIO", "lunes a sábado de 8 a. m. a 6 p. m.")

MARCA_DATOS_PAGO = "[DATOS_DE_PAGO]"
LIMITE_MENSAJE = 350     # límite de Mercado Libre para mensajes de postventa
LIMITE_RESPUESTA = 2000  # límite de Mercado Libre para respuestas a preguntas

_cliente = None


def cliente():
    global _cliente
    if _cliente is None:
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        _cliente = Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
        )
    return _cliente


# ---------------------------------------------------------------------------
# Filtro de seguridad: Mercado Libre sanciona compartir datos de contacto.
# Si el texto trae algo así, NO se publica solo: queda para que lo revises.
# ---------------------------------------------------------------------------
_PATRONES_PROHIBIDOS = [
    (r"https?://|www\.|\b[\w-]+\.(com|net|org|ve|app|me|ly)\b", "enlace"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "correo"),
    (r"\b(whats\s?app|wasap|wsp|telegram|instagram|facebook|tik\s?tok)\b", "red social o mensajería"),
    (r"\b(llam[aeá]nos|llam[aá]me|escr[ií]be(nos|me) al|nuestro (n[uú]mero|tel[eé]fono))\b", "invitación a contacto externo"),
]


def _parece_telefono(texto):
    for m in re.finditer(r"\+?\d[\d\s().-]{8,}\d", texto):
        if len(re.sub(r"\D", "", m.group())) >= 10:
            return True
    return False


def revisar_texto(texto):
    """Devuelve None si el texto es publicable, o el motivo por el que no lo es."""
    for patron, motivo in _PATRONES_PROHIBIDOS:
        if re.search(patron, texto, re.IGNORECASE):
            return f"contiene {motivo}"
    if _parece_telefono(texto):
        return "contiene un número de teléfono"
    return None


def partir_mensaje(texto, limite=LIMITE_MENSAJE):
    """Parte un texto largo en mensajes de hasta `limite` caracteres, sin cortar palabras."""
    partes, actual = [], ""
    for bloque in re.split(r"(?<=[.!?\n])\s+", texto.strip()):
        while len(bloque) > limite:
            corte = bloque.rfind(" ", 0, limite)
            corte = corte if corte > 0 else limite
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(bloque[:corte].strip())
            bloque = bloque[corte:].strip()
        candidato = f"{actual} {bloque}".strip() if actual else bloque
        if len(candidato) <= limite:
            actual = candidato
        else:
            partes.append(actual)
            actual = bloque
    if actual:
        partes.append(actual)
    return [p for p in partes if p]


def _texto_de(respuesta):
    return "".join(b.text for b in respuesta.content if b.type == "text").strip()


def _correr(system, mensajes, herramientas, ejecutar, max_vueltas=6, listo=lambda: False):
    """Bucle de herramientas. Devuelve (texto_final, stop_reason).
    `listo()` permite cortar apenas se llamó la herramienta que entrega el resultado."""
    mensajes = list(mensajes)
    for _ in range(max_vueltas):
        respuesta = cliente().messages.create(
            model=MODELO,
            max_tokens=8000,
            output_config={"effort": "medium"},
            system=system,
            tools=herramientas,
            messages=mensajes,
        )
        if respuesta.stop_reason == "refusal":
            return "", "refusal"
        usos = [b for b in respuesta.content if b.type == "tool_use"]
        if not usos:
            return _texto_de(respuesta), respuesta.stop_reason
        mensajes.append({"role": "assistant", "content": respuesta.content})
        resultados = []
        for uso in usos:
            try:
                resultado = ejecutar(uso.name, uso.input)
                resultados.append({"type": "tool_result", "tool_use_id": uso.id,
                                   "content": json.dumps(resultado, ensure_ascii=False)})
            except Exception as e:  # noqa: BLE001
                resultados.append({"type": "tool_result", "tool_use_id": uso.id,
                                   "content": f"Error: {e}", "is_error": True})
        if listo():
            return _texto_de(respuesta), "listo"
        mensajes.append({"role": "user", "content": resultados})
    return "", "sin_cierre"


# ===========================================================================
# 1) PREGUNTAS EN PUBLICACIONES
# ===========================================================================

SYSTEM_PREGUNTAS = f"""\
Eres quien responde las preguntas de las publicaciones de {NEGOCIO} en \
Mercado Libre Venezuela (repuestos y accesorios para vehículos). Recibes \
la pregunta de un posible comprador y los datos reales de la publicación.

CÓMO RESPONDES:
- Español de Venezuela, cordial y breve: normalmente 1 a 3 oraciones. \
Saluda de forma corta y cierra invitando a comprar cuando tenga sentido \
(ej. "puedes ofertar con confianza").
- Sin errores de ortografía ni tildes. Sin emojis.
- Solo usas los datos de la publicación que te pasan (título, precio, \
stock, atributos, descripción) o lo que devuelva buscar_publicaciones. \
Nunca inventes compatibilidades, medidas, marcas, garantías ni tiempos.

REGLAS DE MERCADO LIBRE (no negociables, su incumplimiento suspende la cuenta):
- Nunca escribas teléfonos, WhatsApp, redes sociales, correos, enlaces ni \
direcciones exactas, ni invites a contactar por fuera de Mercado Libre.
- No ofrezcas precios distintos al publicado ni ventas por fuera.

CUÁNDO PEDIR AYUDA A UN HUMANO (necesita_humano = true):
- La respuesta depende de un dato que no está en la publicación (ej. \
compatibilidad con un vehículo que no aparece, garantía, descuentos).
- Reclamos, amenazas, preguntas sobre una compra ya hecha, o algo raro.
En ese caso igual deja en "respuesta" tu mejor borrador prudente.

Cuando termines, llama SIEMPRE a la herramienta "responder" con el texto final.
"""

HERR_BUSCAR = {
    "name": "buscar_publicaciones",
    "description": "Busca otras publicaciones activas de esta tienda, por si el comprador pregunta por algo que no es esta publicación.",
    "input_schema": {
        "type": "object",
        "properties": {"texto": {"type": "string", "description": "Qué buscar, ej. 'filtro aceite corolla'"}},
        "required": ["texto"],
    },
}

HERR_RESPONDER = {
    "name": "responder",
    "description": "Entrega la respuesta final a la pregunta del comprador.",
    "input_schema": {
        "type": "object",
        "properties": {
            "respuesta": {"type": "string", "description": "Texto a publicar como respuesta (máx. 2000 caracteres)."},
            "necesita_humano": {"type": "boolean", "description": "true si conviene que el dueño la revise antes de publicarla."},
            "motivo": {"type": "string", "description": "Si necesita_humano es true, por qué."},
        },
        "required": ["respuesta", "necesita_humano"],
    },
}


def _resumen_publicacion(item, descripcion):
    atributos = [
        f"{a.get('name')}: {a.get('value_name')}"
        for a in item.get("attributes", []) if a.get("value_name")
    ]
    return json.dumps({
        "titulo": item.get("title"),
        "precio": item.get("price"),
        "moneda": item.get("currency_id"),
        "stock_disponible": item.get("available_quantity"),
        "condicion": item.get("condition"),
        "atributos": atributos[:40],
        "descripcion": (descripcion or "")[:3000],
    }, ensure_ascii=False, indent=1)


def redactar_respuesta(texto_pregunta, item, descripcion, buscar=None):
    """
    Devuelve {"texto", "necesita_humano", "motivo"}.
    `buscar` es la función que busca otras publicaciones (se inyecta desde app_meli).
    """
    final = {}

    def ejecutar(nombre, params):
        if nombre == "buscar_publicaciones":
            return buscar(params.get("texto", "")) if buscar else []
        if nombre == "responder":
            final.update(params)
            return {"ok": True}
        return {"error": f"herramienta desconocida {nombre}"}

    contenido = (
        f"PUBLICACIÓN:\n{_resumen_publicacion(item, descripcion)}\n\n"
        f"PREGUNTA DEL COMPRADOR:\n{texto_pregunta}"
    )
    texto_libre, stop = _correr(
        SYSTEM_PREGUNTAS, [{"role": "user", "content": contenido}],
        [HERR_BUSCAR, HERR_RESPONDER], ejecutar, listo=lambda: "respuesta" in final,
    )

    texto = (final.get("respuesta") or texto_libre or "").strip()[:LIMITE_RESPUESTA]
    necesita = bool(final.get("necesita_humano")) or not texto or stop == "refusal"
    motivo = final.get("motivo") or ("el modelo no dio una respuesta" if not texto else None)

    problema = revisar_texto(texto) if texto else None
    if problema:
        necesita, motivo = True, f"El filtro de seguridad la frenó: {problema}"
    return {"texto": texto, "necesita_humano": necesita, "motivo": motivo}


# ===========================================================================
# 2) POSTVENTA (mensajería de la orden)
# ===========================================================================

SYSTEM_POSTVENTA = f"""\
Eres quien atiende a los compradores de {NEGOCIO} en Mercado Libre \
Venezuela después de que compran, usando la mensajería de la orden. Tu \
trabajo: que el comprador pague y que tengamos sus datos para enviarle \
el pedido, con un trato cálido y profesional.

ESTILO: español de Venezuela, cercano pero cuidado, sin errores ni \
emojis. Mensajes MUY cortos (Mercado Libre corta a 350 caracteres por \
mensaje): una idea por mensaje, directo al punto.

FLUJO:
1. Primer contacto: agradece la compra, confirma el producto y el total, \
y comparte los datos de pago escribiendo exactamente {MARCA_DATOS_PAGO} \
(el sistema lo reemplaza por los datos reales — nunca los escribas tú). \
Pide que al pagar te envíe método, banco, número de referencia, monto y \
fecha, o una captura.
2. Pide los datos de envío: nombre y apellido, cédula, teléfono del que \
recibe, ciudad/estado y la agencia o dirección de la oficina donde \
retira. Opciones de envío: {OPCIONES_ENVIO}
3. Cuando el comprador diga que ya pagó y dé los datos del pago, llama a \
reportar_pago. Si faltan datos clave (referencia o monto), pídeselos \
primero. Luego dile que estamos verificando el pago y que le confirmamos \
en breve. NUNCA confirmes tú que el pago llegó: eso lo hace una persona.
4. Cuando tengas los datos de envío completos, llama a guardar_datos_envio.
5. Si hay un reclamo, quiere cancelar, pide algo fuera de lo normal, se \
molesta o no sabes qué responder, llama a pasar_a_humano y dile con \
amabilidad que un asesor lo atenderá en breve.

REGLAS:
- No inventes precios, tiempos de entrega ni garantías. El horario de \
atención es {HORARIO}.
- No escribas teléfonos, WhatsApp, redes, correos ni enlaces propios, \
ni pidas hablar por fuera de Mercado Libre. Pedirle al comprador SU \
teléfono para la guía de envío sí está permitido.
- Si la orden ya tiene el pago confirmado o fue enviada, solo responde \
dudas sobre el envío; no vuelvas a pedir pago.
"""

HERR_PAGO = {
    "name": "reportar_pago",
    "description": "Registra que el comprador dice haber pagado, para que el dueño lo verifique. Úsala solo cuando el comprador informe su pago.",
    "input_schema": {
        "type": "object",
        "properties": {
            "metodo": {"type": "string", "description": "Pago Móvil, transferencia, Zelle, Binance, efectivo, etc."},
            "banco": {"type": "string", "description": "Banco de origen, si aplica"},
            "referencia": {"type": "string", "description": "Número de referencia o confirmación"},
            "monto": {"type": "string", "description": "Monto pagado con su moneda, ej. 'Bs. 1.250,00' o 'USD 25'"},
            "fecha": {"type": "string", "description": "Fecha del pago, como la dijo el comprador"},
            "titular": {"type": "string", "description": "Nombre del titular que pagó, si lo dio"},
        },
        "required": ["metodo", "referencia", "monto"],
    },
}

HERR_ENVIO = {
    "name": "guardar_datos_envio",
    "description": "Guarda los datos de envío del comprador cuando ya están completos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre": {"type": "string"},
            "cedula": {"type": "string"},
            "telefono": {"type": "string"},
            "ciudad_estado": {"type": "string"},
            "agencia_o_direccion": {"type": "string", "description": "Empresa de envío y oficina, o dirección"},
            "notas": {"type": "string"},
        },
        "required": ["nombre", "cedula", "telefono", "ciudad_estado", "agencia_o_direccion"],
    },
}

HERR_HUMANO = {
    "name": "pasar_a_humano",
    "description": "Pausa las respuestas automáticas de esta orden y avisa al dueño para que atienda en persona.",
    "input_schema": {
        "type": "object",
        "properties": {"motivo": {"type": "string"}},
        "required": ["motivo"],
    },
}


def _historial_a_mensajes(historial):
    mensajes = []
    for m in historial:
        rol = "user" if m["rol"] == "comprador" else "assistant"
        if mensajes and mensajes[-1]["role"] == rol:
            mensajes[-1]["content"] += "\n" + m["texto"]
        else:
            mensajes.append({"role": rol, "content": m["texto"]})
    if mensajes and mensajes[0]["role"] == "assistant":
        mensajes.insert(0, {"role": "user", "content": "(Inicio de la conversación de la orden.)"})
    return mensajes


def contexto_orden(orden):
    return (
        "DATOS DE LA ORDEN (reales):\n"
        f"- Comprador: {orden.get('comprador_nombre') or orden.get('comprador_apodo')}\n"
        f"- Productos: {orden.get('detalle')}\n"
        f"- Total: {orden.get('total')} {orden.get('moneda')}\n"
        f"- Estado actual: {orden.get('estado')}\n"
        f"- Datos de envío ya guardados: {orden.get('datos_envio') or 'ninguno'}"
    )


def conversar_postventa(orden, historial, ejecutar, primer_contacto=False):
    """
    Devuelve el texto (ya con los datos de pago insertados) que hay que
    mandarle al comprador, o "" si no corresponde responder.
    `ejecutar(nombre, params)` corre las herramientas (lo aporta app_meli).
    """
    system = SYSTEM_POSTVENTA + "\n\n" + contexto_orden(orden)
    if primer_contacto:
        mensajes = [{"role": "user", "content": (
            "[AVISO DEL SISTEMA, no es el comprador] Se acaba de concretar esta compra. "
            "Escribe el primer mensaje al comprador según el paso 1 y 2 del flujo."
        )}]
    else:
        mensajes = _historial_a_mensajes(historial)
        if not mensajes or mensajes[-1]["role"] != "user":
            return ""

    texto, stop = _correr(system, mensajes, [HERR_PAGO, HERR_ENVIO, HERR_HUMANO], ejecutar)
    if stop == "refusal":
        ejecutar("pasar_a_humano", {"motivo": "el modelo no quiso responder este mensaje"})
        return ""
    return texto


def insertar_datos_pago(texto):
    if MARCA_DATOS_PAGO in texto:
        return texto.replace(MARCA_DATOS_PAGO, DATOS_PAGO or "(datos de pago: configurar MELI_DATOS_PAGO)")
    return texto
