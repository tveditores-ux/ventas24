"""
Avisos al dueño (nueva venta, pago reportado, caso que necesita humano).

Intenta por WhatsApp — primero WeCall (el canal real), después Twilio —
y siempre deja el aviso guardado en el panel /meli/panel, así que aunque
WhatsApp falle (ej. ventana de 24 h cerrada) no se pierde nada.
"""

import os
import sys

from . import meli_db

DUENO_WHATSAPP = os.environ.get("MELI_DUENO_WHATSAPP", "").strip()  # ej. +584141234567
PANEL_URL = os.environ.get("MELI_PANEL_URL", "").strip()


def _por_wecall(texto):
    if not os.environ.get("WECALL_API_KEY"):
        return False
    # wecall.py vive en la carpeta del proyecto; se importa tal cual, sin modificarlo.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import wecall
    wecall.enviar_mensaje(DUENO_WHATSAPP, texto=texto)
    return True


def _por_twilio(texto):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth = os.environ.get("TWILIO_AUTH_TOKEN")
    desde = os.environ.get("TWILIO_WHATSAPP_FROM")
    if not (sid and auth and desde):
        return False
    from twilio.rest import Client
    Client(sid, auth).messages.create(from_=desde, to=f"whatsapp:{DUENO_WHATSAPP}", body=texto)
    return True


def avisar(texto):
    if PANEL_URL:
        texto = f"{texto}\n\nPanel: {PANEL_URL}"

    canal = "solo_panel"
    if DUENO_WHATSAPP:
        for nombre, funcion in (("wecall", _por_wecall), ("twilio", _por_twilio)):
            try:
                if funcion(texto):
                    canal = nombre
                    break
            except Exception as e:  # noqa: BLE001 — un aviso fallido no debe tumbar el flujo
                print(f"  ⚠️ aviso por {nombre} falló: {e}")

    meli_db.registrar_aviso(texto, canal)
    print(f"  🔔 aviso ({canal}): {texto.splitlines()[0]}")
    return canal
