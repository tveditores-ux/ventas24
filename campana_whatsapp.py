"""
Manda una plantilla de WhatsApp aprobada a los clientes al mayor con
estado "nuevo" en crm.db, ofreciéndoles precios al mayor.

IMPORTANTE - Requisito previo (lo hacés vos, una sola vez):
1. En Twilio Console > Messaging > Content Template Builder, creá una
   plantilla de WhatsApp (ej. con una variable {{1}} para el nombre del
   negocio) y mandala a aprobar con Meta. La aprobación puede tardar
   hasta 24 horas.
2. Una vez aprobada, copiá su "Content SID" (empieza con HX...) y
   agregalo a .env como TWILIO_TEMPLATE_SID.

Sin una plantilla aprobada, Twilio/Meta van a rechazar estos envíos:
no se puede escribirle "en frío" a un número por WhatsApp.

Uso:
    python campana_whatsapp.py            # modo prueba: solo imprime qué mandaría
    python campana_whatsapp.py --enviar   # manda de verdad y marca "contactado"
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from twilio.rest import Client

import db

load_dotenv()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_TEMPLATE_SID = os.environ.get("TWILIO_TEMPLATE_SID")


def main():
    enviar_de_verdad = "--enviar" in sys.argv

    pendientes = db.listar_mayoristas(estado="nuevo")

    if not pendientes:
        print("No hay clientes al mayor con estado 'nuevo' en crm.db.")
        return

    if enviar_de_verdad and not TWILIO_TEMPLATE_SID:
        print("⚠️  Falta TWILIO_TEMPLATE_SID en .env (necesitás una plantilla aprobada por Meta). No mandé nada.")
        return

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if enviar_de_verdad else None

    print(f"{'ENVIANDO' if enviar_de_verdad else 'MODO PRUEBA (nada se envía)'} — {len(pendientes)} cliente(s) nuevo(s):\n")

    for c in pendientes:
        telefono = c["telefono"].strip()
        destino = telefono if telefono.startswith("whatsapp:") else f"whatsapp:{telefono}"
        print(f"  → {c['negocio']} ({c['nombre']}) — {telefono}")

        if enviar_de_verdad:
            client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=destino,
                content_sid=TWILIO_TEMPLATE_SID,
                content_variables=f'{{"1":"{c["negocio"]}"}}',
            )
            nota = (c.get("notas") or "") + f" | WhatsApp enviado {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            db.actualizar_contacto(c["id"], estado="contactado", notas=nota.strip(" |"))

    if enviar_de_verdad:
        print("\n✅ Enviado y estado actualizado en crm.db (nuevo → contactado).")
    else:
        print("\nEsto fue una simulación. Corré con --enviar para mandar de verdad (requiere TWILIO_TEMPLATE_SID aprobado).")


if __name__ == "__main__":
    main()
