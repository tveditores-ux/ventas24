"""
Servidor web que conecta el Agente a WhatsApp.

Dos canales:
  - Twilio (sandbox/número propio) — ver /whatsapp. Se deja andando como
    respaldo; WeCall Inbox es el canal real del negocio.
  - WeCall Inbox (WhatsApp Business real vía Meta Cloud API) — ver
    /webhook/wecall. Además de recibir el webhook, hay un hilo de
    polling de respaldo (revisa mensajes nuevos cada cierto intervalo)
    porque el webhook de WeCall es "best-effort" sin reintentos.

Uso:
    python app_whatsapp.py

Requiere en .env: ANTHROPIC_API_KEY, y según el canal, TWILIO_ACCOUNT_SID/
TWILIO_AUTH_TOKEN/TWILIO_WHATSAPP_FROM y/o WECALL_API_KEY/WECALL_WEBHOOK_SECRET.

El webhook responde de inmediato (TwiML vacío o 200 vacío según el canal)
y procesa el mensaje del agente en un hilo aparte, mandando la respuesta
real por la API correspondiente cuando esté lista. Esto evita que el
webhook corte la conexión si el agente (que hace varias llamadas a la
API de Anthropic) tarda más de lo que el remitente espera por una
respuesta síncrona.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify
from twilio.rest import Client

import db
import wecall
from wecall import VentanaCerradaError, EnvioFallidoError
from agente import Agente

load_dotenv()

app = Flask(__name__)


@app.route("/debug/version", methods=["GET"])
def debug_version():
    return jsonify({"commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "desconocido")})


@app.route("/debug/wecall-key-check", methods=["POST"])
def debug_wecall_key_check():
    """DIAGNÓSTICO TEMPORAL — nunca expone las claves reales. Solo dice si
    el valor que mandaste coincide EXACTO con lo que el proceso tiene
    cargado, para descartar espacios/saltos de línea extra sin filtrar
    el secreto por HTTP."""
    import hmac as _hmac
    datos = request.get_json(silent=True) or {}
    real_key = os.environ.get("WECALL_API_KEY") or ""
    real_secret = os.environ.get("WECALL_WEBHOOK_SECRET") or ""
    resultado = {}
    if "api_key" in datos:
        candidato = datos["api_key"]
        resultado["api_key_coincide"] = _hmac.compare_digest(candidato, real_key)
        resultado["api_key_longitud_real"] = len(real_key)
        resultado["api_key_longitud_recibida"] = len(candidato)
    if "webhook_secret" in datos:
        candidato = datos["webhook_secret"]
        resultado["webhook_secret_coincide"] = _hmac.compare_digest(candidato, real_secret)
        resultado["webhook_secret_longitud_real"] = len(real_secret)
        resultado["webhook_secret_longitud_recibida"] = len(candidato)
    return jsonify(resultado)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
    else None
)

# Serializa el chequeo "¿ya procesé este mensaje de WeCall?" entre el
# webhook y el polling de respaldo, para no mandar la respuesta dos veces.
wecall_lock = threading.Lock()


def procesar_y_responder(numero: str, mensaje: str):
    try:
        # El historial vive en crm.db, así que un Agente nuevo por mensaje
        # ya arranca con todo el contexto de conversaciones anteriores.
        agente = Agente(telefono=numero)
        respuesta = agente.procesar_mensaje(mensaje)
        print(f"🤖 → {numero}: {respuesta}")
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=numero,
            body=respuesta,
        )
    except Exception as e:
        print(f"❌ Error procesando mensaje de {numero}: {e}")


def _mapear_historial_wecall(mensajes, excluir_id=None):
    """Convierte los mensajes de WeCall al formato {role, content} que usa el agente."""
    historial = []
    for m in mensajes:
        if excluir_id is not None and m.get("id") == excluir_id:
            continue
        rol = "user" if m.get("direccion") == "entrante" else "assistant"
        texto = m.get("texto") or "[archivo adjunto]"
        historial.append({"role": rol, "content": texto})
    return historial


def procesar_mensaje_wecall(telefono: str, mensaje: dict):
    """Procesa un mensaje entrante de WeCall (llamado desde el webhook o el polling)."""
    mensaje_id = mensaje.get("id")
    texto_mensaje = (mensaje.get("texto") or "").strip()
    if mensaje.get("direccion") != "entrante" or not texto_mensaje or mensaje_id is None:
        return

    telefono = db.normalizar_telefono(telefono)

    # Marca el mensaje como visto ANTES de procesarlo (dentro del lock) para
    # que si el webhook y el polling lo agarran casi al mismo tiempo, el
    # segundo lo vea ya procesado y no mande la respuesta dos veces. Por
    # eso todo lo que sigue va en un try/except amplio: si algo falla acá
    # abajo, el mensaje ya quedó marcado como visto y NO se va a reintentar
    # solo — hace falta que quede un log claro de qué pasó.
    with wecall_lock:
        contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
        with db.conectar() as con:
            fila = con.execute(
                "SELECT wecall_ultimo_id FROM contactos WHERE id = ?", (contacto_id,)
            ).fetchone()
        ya_visto = fila["wecall_ultimo_id"] if fila else None
        if ya_visto is not None and ya_visto >= mensaje_id:
            return
        db.actualizar_contacto(contacto_id, wecall_ultimo_id=mensaje_id)

    try:
        try:
            contexto = wecall.obtener_contexto(telefono, limite=30)
        except Exception as e:
            print(f"  (wecall) no pude traer contexto de {telefono}: {e}")
            contexto = None

        historial_previo = None
        if contexto:
            nombre = (contexto.get("contacto") or {}).get("nombre")
            if nombre:
                db.actualizar_contacto(contacto_id, nombre=nombre)
            historial_previo = _mapear_historial_wecall(contexto.get("mensajes", []), excluir_id=mensaje_id)

        print(f"📩 (wecall) {telefono}: {texto_mensaje}")
        agente = Agente(telefono=telefono)
        respuesta = agente.procesar_mensaje(texto_mensaje, historial_previo=historial_previo)
        print(f"🤖 (wecall) → {telefono}: {respuesta}")

        try:
            wecall.enviar_mensaje(telefono, texto=respuesta)
        except VentanaCerradaError:
            print(f"  ⚠️ (wecall) ventana de 24h cerrada para {telefono} — hace falta responder con una plantilla aprobada")
        except EnvioFallidoError as e:
            print(f"  ❌ (wecall) Meta rechazó el envío a {telefono}: {e.detalle}")
    except Exception:
        import traceback
        print(f"  ❌ (wecall) error procesando mensaje {mensaje_id} de {telefono} (ya quedó marcado como visto, no se reintenta solo):")
        traceback.print_exc()


@app.route("/webhook/wecall", methods=["POST"])
def wecall_webhook():
    cuerpo_crudo = request.get_data()
    firma = request.headers.get("X-WeCall-Signature", "")

    # Log del body crudo ANTES de cualquier validación — así queda
    # evidencia de que el webhook llegó aunque la firma o el evento no
    # sean los esperados.
    print(f"🔔 (wecall) webhook recibido, {len(cuerpo_crudo)} bytes: {cuerpo_crudo[:500]!r}")

    if not wecall.verificar_firma(cuerpo_crudo, firma):
        print(f"  ⚠️ (wecall) firma inválida en el webhook, ignorado (header recibido: {firma!r})")
        return ("firma inválida", 401)

    payload = request.get_json(silent=True) or {}
    evento = payload.get("evento")
    if evento == "mensaje_nuevo":
        contacto = payload.get("contacto") or {}
        mensaje = payload.get("mensaje") or {}
        telefono = contacto.get("telefono", "")
        if telefono and mensaje:
            threading.Thread(
                target=procesar_mensaje_wecall, args=(telefono, mensaje), daemon=True
            ).start()
        else:
            print(f"  ⚠️ (wecall) evento 'mensaje_nuevo' sin teléfono o sin mensaje: {payload}")
    else:
        print(f"  (wecall) webhook con evento distinto de 'mensaje_nuevo': {evento!r} — ignorado")

    # Responder rápido — WeCall solo espera 6s y no reintenta.
    return ("", 200)


@app.route("/debug/wecall-test/<telefono>", methods=["GET"])
def wecall_debug_test(telefono):
    """DIAGNÓSTICO TEMPORAL — no manda nada, solo corre la misma cadena de
    procesamiento de forma síncrona y devuelve el error real si lo hay,
    en vez de que se pierda en un hilo en segundo plano."""
    import traceback
    telefono = db.normalizar_telefono(telefono)
    salida = {"telefono": telefono}
    try:
        contexto = wecall.obtener_contexto(telefono, limite=30)
        salida["contexto_ok"] = contexto is not None
        salida["mensajes_en_contexto"] = len(contexto.get("mensajes", [])) if contexto else 0
        historial_previo = _mapear_historial_wecall(contexto.get("mensajes", [])) if contexto else None
        salida["historial_previo"] = historial_previo
        agente = Agente(telefono=telefono)
        respuesta = agente.procesar_mensaje("prueba de diagnóstico, ignora este mensaje y responde solo 'ok'", historial_previo=historial_previo)
        salida["respuesta_agente"] = respuesta
        salida["exito"] = True
    except Exception as e:
        salida["exito"] = False
        salida["error_tipo"] = type(e).__name__
        salida["error_msg"] = str(e)
        salida["traceback"] = traceback.format_exc()
    return jsonify(salida)


@app.route("/debug/wecall-reset-cursor/<telefono>", methods=["POST"])
def wecall_debug_reset_cursor(telefono):
    """DIAGNÓSTICO TEMPORAL — para deshacer el atasco que dejan mensajes
    sintéticos de prueba con id muy alto: baja wecall_ultimo_id al id
    real que se indique (por query ?hasta=N), para que el polling y el
    webhook puedan volver a procesar mensajes reales que quedaron
    bloqueados por el chequeo de duplicados."""
    hasta = request.args.get("hasta", type=int)
    if hasta is None:
        return jsonify({"error": "falta ?hasta=<id>"}), 400
    telefono = db.normalizar_telefono(telefono)
    contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
    db.actualizar_contacto(contacto_id, wecall_ultimo_id=hasta)
    return jsonify({"telefono": telefono, "contacto_id": contacto_id, "wecall_ultimo_id_nuevo": hasta})


@app.route("/api/wecall/enlace/<telefono>", methods=["GET"])
def wecall_enlace_conversacion(telefono):
    """Para el botón 'Ver WhatsApp' del CRM: arma el link al chat completo en WeCall."""
    try:
        datos = wecall.generar_enlace_conversacion(db.normalizar_telefono(telefono), minutos_validez=60)
        return jsonify(datos)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _loop_polling_wecall():
    """Respaldo del webhook: revisa cada tanto si algún contacto tiene mensajes
    nuevos que el webhook no haya avisado (es 'best-effort', sin reintentos)."""
    intervalo = int(os.environ.get("WECALL_POLL_INTERVAL_SEGUNDOS", 90))
    con_error = set()  # teléfonos que ya fallaron en el ciclo anterior, para no repetir el aviso
    while True:
        time.sleep(intervalo)
        try:
            for c in db.listar_contactos_wecall():
                try:
                    nuevos = wecall.obtener_mensajes_nuevos(c["telefono"], desde_id=c["wecall_ultimo_id"])
                    con_error.discard(c["telefono"])
                except Exception as e:
                    if c["telefono"] not in con_error:
                        print(f"  (wecall-poll) error consultando {c['telefono']}: {e}")
                        con_error.add(c["telefono"])
                    continue
                for m in sorted(nuevos, key=lambda x: x.get("id", 0)):
                    if m.get("direccion") == "entrante":
                        procesar_mensaje_wecall(c["telefono"], m)
                    else:
                        # Mensaje saliente que no pasó por nuestro propio envío
                        # (ej. alguien contestó a mano desde WeCall): solo
                        # avanzamos el cursor para no volver a revisarlo.
                        mid = m.get("id")
                        if mid is not None:
                            with wecall_lock:
                                db.actualizar_contacto(c["id"], wecall_ultimo_id=mid)
        except Exception as e:
            print(f"  (wecall-poll) error en el ciclo: {e}")


if os.environ.get("WECALL_API_KEY") and os.environ.get("WECALL_WEBHOOK_SECRET"):
    threading.Thread(target=_loop_polling_wecall, daemon=True).start()


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    numero = request.form.get("From", "")
    mensaje = request.form.get("Body", "").strip()

    print(f"📩 {numero}: {mensaje}")

    if mensaje and twilio_client:
        threading.Thread(
            target=procesar_y_responder, args=(numero, mensaje), daemon=True
        ).start()

    # Responder a Twilio de inmediato (vacío); la respuesta real se manda
    # por la API REST cuando el agente termine de procesar.
    return Response("<Response></Response>", mimetype="text/xml")


if __name__ == "__main__":
    # Para producción, Render corre esto con gunicorn (ver Procfile), no
    # con este servidor de desarrollo. Este bloque es solo para pruebas
    # locales directas (python app_whatsapp.py).
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  No encontré ANTHROPIC_API_KEY en .env")
    elif not twilio_client:
        print("⚠️  No encontré TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en .env")
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
