"""
Canal de voz de Ventas 24: el mismo vendedor de texto (Salomón), pero hablando por teléfono.

    Twilio ──llamada──► /voz/entrante ──► saludo + <Gather speech>
                         /voz/turno    ──► SalomonVoz.responder() ──► <Say> + <Gather> (o <Hangup>)
                         /voz/estado   ──► cierre: resumen de la llamada

Tres piezas propias del canal, además de todo lo que ya tiene Salomón (catálogo, pedidos, lista de espera,
equipo, vigilante de reglas):

  * Cerebro de voz (voz_cerebro.md): se suma a salomon_cerebro.md y manda en todo lo que sea forma.
  * Memoria temporal: la transcripción de la llamada vive en `interacciones` (la misma que usa el chat),
    así que lo que se habla por teléfono lo sabe el WhatsApp y al revés. Además, al empezar la llamada
    se arma un resumen de lo que ya se sabe del cliente (pedidos, solicitudes abiertas, lista de espera),
    y al terminar queda un resumen de la llamada.
  * Vigilante de voz (voz_vigilante.py): respuesta corta, sin formato, sin datos de pago dictados.

Los datos de pago y el resumen del pedido NUNCA se dictan: salen por WhatsApp (ciclo.notificar_cliente).
Si el WhatsApp no puede salir (ventana de 24 h cerrada), se abre una solicitud para que el equipo escriba.

Variables de entorno (ver .env.example):
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN   firma de los webhooks y llamadas salientes
    TWILIO_VOICE_FROM                        número de Twilio con voz
    PUBLIC_BASE_URL                          https://api.ventas-24.com (para validar la firma y armar callbacks)
    VOZ_MODELO       modelo de la llamada (por defecto uno rápido: en el teléfono cada segundo se nota)
    VOZ_VOZ          voz de Twilio (por defecto Polly.Andres-Neural; verifica en tu consola cuáles tienes)
    VOZ_IDIOMA       idioma de reconocimiento y de la voz (por defecto es-MX)
    VOZ_VIGILANTE_LLM=1   enciende el revisor con modelo (más lento)
"""

import json
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape

import db
import eventos
import salomon
import voz_vigilante

VOZ = os.environ.get("VOZ_VOZ", "Polly.Andres-Neural")
IDIOMA = os.environ.get("VOZ_IDIOMA", "es-MX")
MODELO_VOZ = os.environ.get("VOZ_MODELO", "claude-haiku-4-5-20251001")
MAX_TURNOS = 40
MAX_SILENCIOS = 2
MAX_INCOMPRENDIDOS = 2
CONFIANZA_MINIMA = 0.35
CEREBRO_VOZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voz_cerebro.md")

DERIVACION_VOZ = (
    "Dame un momento, voy a pasar tu consulta a una persona del equipo y te escribe por WhatsApp "
    "para responderte con exactitud. Gracias por llamar."
)

HERRAMIENTA_CERRAR = {
    "name": "cerrar_llamada",
    "description": (
        "Termina la llamada después de despedirte. Úsala cuando el cliente se despida o ya no quede nada por "
        "resolver. Escribe primero tu despedida como respuesta, y llama a esta herramienta en ese mismo turno."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS voz_llamadas (
    callsid TEXT PRIMARY KEY,
    contacto_id INTEGER REFERENCES contactos(id),
    direccion TEXT NOT NULL DEFAULT 'entrante',     -- entrante / saliente / simulada
    inicio TEXT DEFAULT CURRENT_TIMESTAMP,
    fin TEXT,
    turnos INTEGER NOT NULL DEFAULT 0,
    silencios INTEGER NOT NULL DEFAULT 0,
    incomprendidos INTEGER NOT NULL DEFAULT 0,
    estado TEXT NOT NULL DEFAULT 'en_curso',         -- en_curso / terminada / derivada
    resumen TEXT
);
"""


def asegurar_tablas():
    with db.conectar() as con:
        con.executescript(ESQUEMA)


# ---------------------------------------------------------------------
# Memoria de la llamada
# ---------------------------------------------------------------------

def contexto_del_cliente(contacto_id: int) -> str:
    """Lo que ya se sabe de este cliente, para que el vendedor lo use sin exhibirlo."""
    partes = []
    with db.conectar() as con:
        c = con.execute("SELECT nombre, tipo, notas FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        pedidos = con.execute(
            "SELECT producto, cantidad, total, estado_pago, substr(fecha,1,10) AS f FROM pedidos "
            "WHERE contacto_id = ? AND estado != 'cancelado' ORDER BY id DESC LIMIT 5", (contacto_id,)
        ).fetchall()
        espera = con.execute(
            "SELECT producto FROM solicitudes_espera WHERE contacto_id = ? AND atendido = 0 LIMIT 5", (contacto_id,)
        ).fetchall()
        abiertas = con.execute(
            "SELECT asunto FROM solicitudes_equipo WHERE contacto_id = ? AND estado = 'abierta' LIMIT 3", (contacto_id,)
        ).fetchall()
        previas = con.execute(
            "SELECT resumen, substr(inicio,1,10) AS f FROM voz_llamadas WHERE contacto_id = ? AND resumen IS NOT NULL "
            "ORDER BY inicio DESC LIMIT 2", (contacto_id,)
        ).fetchall()
    if c and c["nombre"]:
        partes.append(f"Se llama {c['nombre']}.")
    if c and c["notas"]:
        partes.append("Notas del equipo: " + c["notas"][-300:].replace("\n", " "))
    if pedidos:
        partes.append("Últimos pedidos: " + "; ".join(
            f"{p['cantidad']} x {p['producto'][:50]} ({p['f']}, {p['estado_pago']})" for p in pedidos))
    if espera:
        partes.append("En lista de espera: " + ", ".join(e["producto"][:50] for e in espera))
    if abiertas:
        partes.append("Solicitudes abiertas con el equipo: " + ", ".join(a["asunto"][:60] for a in abiertas))
    if previas:
        partes.append("Llamadas anteriores: " + " | ".join(f"{p['f']}: {p['resumen'][:200]}" for p in previas))
    return " ".join(partes) or "Es la primera vez que habla con nosotros."


def _saludo(contacto_id: int, saliente: bool = False) -> str:
    h = datetime.now().hour
    parte = "buenos días" if h < 12 else "buenas tardes" if h < 19 else "buenas noches"
    with db.conectar() as con:
        c = con.execute("SELECT nombre FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
    nombre = ((c["nombre"] or "").strip().split() or [""])[0] if c else ""
    if saliente:
        return f"Hola{', ' + nombre if nombre else ''}, {parte}. Te llamo de Ventas 24. ¿Tienes un minuto para hablar de repuestos?"
    return f"Hola{', ' + nombre if nombre else ''}, {parte}. Habla Salomón, de Ventas 24. ¿En qué te puedo ayudar?"


# ---------------------------------------------------------------------
# El vendedor de voz
# ---------------------------------------------------------------------

class SalomonVoz(salomon.Salomon):
    """Salomón con el cerebro, el vigilante y las herramientas del canal de voz."""

    def __init__(self, *args, simulacion=False, **kwargs):
        super().__init__(*args, **kwargs)
        with open(CEREBRO_VOZ, encoding="utf-8") as f:
            self.system_prompt = self.system_prompt + "\n\n" + f.read()
        self.modelo = MODELO_VOZ
        self.max_tokens = 400            # una respuesta hablada es corta
        self.mensaje_derivacion = DERIVACION_VOZ
        self.simulacion = simulacion
        self.colgar = False
        self.whatsapp_enviados = []      # lo que salió (o saldría, en simulación) por WhatsApp

    def preparar(self, callsid: str):
        self.callsid = callsid
        self.system_prompt += (
            "\n\n# Lo que ya sabes de este cliente (úsalo sin exhibirlo; nunca digas «veo en tu historial»)\n"
            + contexto_del_cliente(self.contacto_id)
        )

    def _herramientas(self):
        return super()._herramientas() + [HERRAMIENTA_CERRAR]

    def _ejecutar_herramienta(self, nombre, params):
        if nombre == "cerrar_llamada":
            self.colgar = True
            return {"mensaje": "La llamada se cierra después de tu despedida."}
        return super()._ejecutar_herramienta(nombre, params)

    def _vigilar(self, texto, historial, equipo_avisado):
        v = salomon.vigilante.revisar_reglas(texto, self._precios, self._registro_hecho, equipo_avisado)
        if v.ok:
            v = voz_vigilante.revisar(texto)
        if v.ok and os.environ.get("VOZ_VIGILANTE_LLM") == "1":
            v = salomon.vigilante.revisar_con_modelo(self.client, self.modelo_vigilante, texto, historial, self._resultados)
        return v

    def _guardar(self, texto_usuario, texto):
        # Mismo historial que el chat, marcado como llamada para que quede claro de dónde salió.
        db.registrar_interaccion(self.contacto_id, "user", "[llamada] " + texto_usuario)
        db.registrar_interaccion(self.contacto_id, "assistant", "[llamada] " + texto)
        return texto

    def _derivar(self, motivo):
        super()._derivar(motivo)
        self.colgar = True
        with db.conectar() as con:
            con.execute("UPDATE voz_llamadas SET estado = 'derivada' WHERE callsid = ?", (getattr(self, "callsid", ""),))

    # -- avisos por WhatsApp (nunca por voz) -----------------------------
    def enviar_whatsapp_pendientes(self):
        import ciclo
        for texto in self.mensajes_posteriores:
            self.whatsapp_enviados.append(texto)
            if self.simulacion:
                continue
            ok, motivo = ciclo.notificar_cliente(self.contacto_id, texto)
            if not ok:
                db.crear_solicitud(self.contacto_id, "pago", "Enviar por WhatsApp el resumen y los datos de pago (pedido por llamada)",
                                   f"El cliente hizo un pedido por teléfono y no pude escribirle ({motivo}).\n\n{texto}")
        self.mensajes_posteriores = []


def _historial_para_modelo(contacto_id: int) -> list:
    h = db.obtener_historial(contacto_id)[-24:]
    while h and h[0]["role"] != "user":
        h = h[1:]
    return h


def responder(telefono: str, callsid: str, texto: str, simulacion=False, direccion="entrante") -> dict:
    """Un turno de la llamada. Devuelve {texto (para el sintetizador), crudo, colgar, whatsapp}."""
    asegurar_tablas()
    ag = SalomonVoz(telefono=telefono, tipo="mayorista", simulacion=simulacion)
    with db.conectar() as con:
        con.execute("INSERT OR IGNORE INTO voz_llamadas (callsid, contacto_id, direccion) VALUES (?, ?, ?)",
                    (callsid, ag.contacto_id, "simulada" if simulacion else direccion))
        fila = con.execute("SELECT turnos FROM voz_llamadas WHERE callsid = ?", (callsid,)).fetchone()
    ag.preparar(callsid)
    eventos.registrar("voz_turno", telefono, texto[:80], agente_tipo=ag.tipo)

    if fila["turnos"] >= MAX_TURNOS:
        crudo = "Se nos está alargando la llamada. Te escribo por WhatsApp para seguir con calma. Gracias por llamar."
        ag.colgar = True
    else:
        crudo = ag.procesar_mensaje(texto, historial_previo=_historial_para_modelo(ag.contacto_id))
    ag.enviar_whatsapp_pendientes()
    with db.conectar() as con:
        con.execute("UPDATE voz_llamadas SET turnos = turnos + 1 WHERE callsid = ?", (callsid,))
    return {"texto": voz_vigilante.para_hablar(crudo), "crudo": crudo, "colgar": ag.colgar,
            "whatsapp": ag.whatsapp_enviados, "contacto_id": ag.contacto_id}


def contar(callsid: str, campo: str) -> int:
    """Suma uno a silencios o incomprendidos y devuelve el total."""
    assert campo in ("silencios", "incomprendidos")
    with db.conectar() as con:
        con.execute(f"UPDATE voz_llamadas SET {campo} = {campo} + 1 WHERE callsid = ?", (callsid,))
        return con.execute(f"SELECT {campo} AS n FROM voz_llamadas WHERE callsid = ?", (callsid,)).fetchone()["n"]


def reiniciar_contadores(callsid: str):
    with db.conectar() as con:
        con.execute("UPDATE voz_llamadas SET silencios = 0, incomprendidos = 0 WHERE callsid = ?", (callsid,))


def iniciar(telefono: str, callsid: str, saliente=False) -> str:
    """Registra la llamada y devuelve el saludo."""
    asegurar_tablas()
    cid = db.obtener_o_crear_contacto(telefono, tipo="mayorista")
    with db.conectar() as con:
        con.execute("INSERT OR IGNORE INTO voz_llamadas (callsid, contacto_id, direccion) VALUES (?, ?, ?)",
                    (callsid, cid, "saliente" if saliente else "entrante"))
    eventos.registrar("voz_inicio", telefono, "llamada " + ("saliente" if saliente else "entrante"))
    saludo = _saludo(cid, saliente)
    db.registrar_interaccion(cid, "assistant", "[llamada] " + saludo)
    return saludo


def terminar(callsid: str, segundos: int | None = None):
    """Cierra la llamada y deja un resumen en la ficha del cliente. Nunca debe romper nada."""
    asegurar_tablas()
    try:
        with db.conectar() as con:
            v = con.execute("SELECT * FROM voz_llamadas WHERE callsid = ?", (callsid,)).fetchone()
            if not v or v["fin"]:
                return
            msgs = con.execute(
                "SELECT rol, mensaje FROM interacciones WHERE contacto_id = ? AND mensaje LIKE '[llamada]%' "
                "AND fecha >= ? ORDER BY id", (v["contacto_id"], v["inicio"])
            ).fetchall()
        dichos = [m["mensaje"][10:].strip() for m in msgs if m["rol"] == "user"]
        resumen = (f"Llamada de {segundos // 60 if segundos else '?'} min, {v['turnos']} turnos. "
                   + ("El cliente dijo: " + " / ".join(d[:100] for d in dichos[:4]) if dichos else "Sin conversación."))
        with db.conectar() as con:
            con.execute("UPDATE voz_llamadas SET fin = CURRENT_TIMESTAMP, resumen = ?, estado = CASE WHEN estado = 'en_curso' THEN 'terminada' ELSE estado END WHERE callsid = ?",
                        (resumen, callsid))
        eventos.registrar("voz_fin", "-", resumen[:80])
    except Exception as e:
        print(f"  (voz) no pude cerrar la llamada {callsid}: {e}")


# ---------------------------------------------------------------------
# TwiML
# ---------------------------------------------------------------------

def _say(texto: str) -> str:
    return f'<Say voice="{VOZ}" language="{IDIOMA}">{escape(texto)}</Say>'


def twiml_hablar(texto: str, url_turno: str, colgar=False) -> str:
    """Dice el texto y, si la llamada sigue, escucha la respuesta del cliente."""
    if colgar:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{_say(texto)}<Hangup/></Response>'
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" language="{IDIOMA}" speechTimeout="auto" speechModel="phone_call" enhanced="true" '
        f'bargeIn="true" actionOnEmptyResult="true" method="POST" action="{escape(url_turno)}">'
        f'{_say(texto)}</Gather>'
        f'<Redirect method="POST">{escape(url_turno)}</Redirect></Response>'
    )


def firma_valida(req) -> bool:
    """Verifica que la petición venga de Twilio (X-Twilio-Signature). Sin TWILIO_AUTH_TOKEN, nada pasa."""
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        return False
    from twilio.request_validator import RequestValidator
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or req.url_root.rstrip("/")
    url = base + req.full_path.rstrip("?")
    return RequestValidator(token).validate(url, req.form, req.headers.get("X-Twilio-Signature", ""))


def llamar(a_telefono: str) -> str:
    """Llamada saliente de prueba o seguimiento (solo administración, y solo a quien lo espera)."""
    from twilio.rest import Client
    sid, token, desde = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"), os.environ.get("TWILIO_VOICE_FROM")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not (sid and token and desde and base):
        raise ValueError("faltan TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VOICE_FROM o PUBLIC_BASE_URL")
    llamada = Client(sid, token).calls.create(
        to=db.normalizar_telefono(a_telefono), from_=desde,
        url=f"{base}/voz/entrante?saliente=1", method="POST",
        status_callback=f"{base}/voz/estado", status_callback_event=["completed"], status_callback_method="POST",
    )
    return llamada.sid
