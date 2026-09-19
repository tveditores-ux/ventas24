"""
Servidor web que conecta el Agente a WhatsApp vía el webhook de Twilio.

Uso:
    python app_whatsapp.py

Requiere en .env: ANTHROPIC_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
y TWILIO_WHATSAPP_FROM (el número de Twilio, ej. "whatsapp:+14155238886").
También requiere que el puerto 5000 esté expuesto públicamente (ej. con
ngrok) para que Twilio pueda llamar a este servidor.

El webhook responde a Twilio de inmediato con un TwiML vacío y procesa
el mensaje del agente en un hilo aparte, mandando la respuesta real por
la API REST de Twilio cuando esté lista. Esto evita que Twilio corte la
conexión si el agente (que hace varias llamadas a la API de Anthropic)
tarda más de lo que Twilio espera por una respuesta síncrona.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import Flask, request, Response
from twilio.rest import Client

from agente import Agente

load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
    else None
)


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
