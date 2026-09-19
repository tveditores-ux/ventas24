"""
Llama por teléfono a los clientes al mayor con estado "nuevo" en crm.db
y les reproduce un mensaje de voz ofreciendo precios al mayor.

IMPORTANTE - Requisito previo (lo hacés vos, una sola vez):
El número de WhatsApp Sandbox (+14155238886) NO sirve para llamadas de
voz. Necesitás un número de Twilio propio con capacidad de voz:
1. En Twilio Console > Phone Numbers > Buy a number, comprá uno (tiene
   costo mensual, distinto del sandbox gratis de WhatsApp).
2. Agregalo a .env como TWILIO_VOICE_FROM (formato +58... o el que sea).

Las llamadas salientes también tienen costo por minuto en Twilio.

Uso:
    python llamar_mayoristas.py            # modo prueba: solo imprime a quién llamaría
    python llamar_mayoristas.py --enviar   # llama de verdad y marca "contactado"
"""

import os
import sys
from datetime import datetime
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from twilio.rest import Client

import db

load_dotenv()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_VOICE_FROM = os.environ.get("TWILIO_VOICE_FROM")

MENSAJE = (
    "Hola, le escribimos de Ventas 24. Tenemos precios especiales al mayor "
    "en filtros de aceite y de aire para talleres y negocios como el suyo. "
    "Si le interesa, puede escribirnos por WhatsApp al número que le llamó. Gracias."
)


def main():
    enviar_de_verdad = "--enviar" in sys.argv

    pendientes = db.listar_mayoristas(estado="nuevo")

    if not pendientes:
        print("No hay clientes al mayor con estado 'nuevo' en crm.db.")
        return

    if enviar_de_verdad and not TWILIO_VOICE_FROM:
        print("⚠️  Falta TWILIO_VOICE_FROM en .env (necesitás un número de Twilio con voz). No llamé a nadie.")
        return

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if enviar_de_verdad else None
    twiml = f'<Response><Say language="es-MX">{escape(MENSAJE)}</Say></Response>'

    print(f"{'LLAMANDO' if enviar_de_verdad else 'MODO PRUEBA (nadie recibe llamada)'} — {len(pendientes)} cliente(s) nuevo(s):\n")

    for c in pendientes:
        telefono = c["telefono"].strip()
        print(f"  → {c['negocio']} ({c['nombre']}) — {telefono}")

        if enviar_de_verdad:
            client.calls.create(to=telefono, from_=TWILIO_VOICE_FROM, twiml=twiml)
            nota = (c.get("notas") or "") + f" | Llamada realizada {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            db.actualizar_contacto(c["id"], estado="contactado", notas=nota.strip(" |"))

    if enviar_de_verdad:
        print("\n✅ Llamadas realizadas y estado actualizado en crm.db (nuevo → contactado).")
    else:
        print("\nEsto fue una simulación. Corré con --enviar para llamar de verdad (requiere TWILIO_VOICE_FROM).")


if __name__ == "__main__":
    main()
