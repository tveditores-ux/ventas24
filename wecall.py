"""
Cliente de la API de WeCall Inbox — el sistema externo que administra
nuestro WhatsApp Business real (Meta Cloud API). Este módulo solo habla
con esa API; no decide nada de negocio (eso lo hace agente.py).

Todas las llamadas van autenticadas con WECALL_API_KEY. La verificación
de firma del webhook usa WECALL_WEBHOOK_SECRET. Ninguna de las dos viaja
al navegador — esto corre siempre del lado del servidor.
"""

import hashlib
import hmac
import os

import requests

WECALL_BASE_URL = os.environ.get("WECALL_BASE_URL", "https://web-production-4ea08.up.railway.app")
WECALL_API_KEY = os.environ.get("WECALL_API_KEY")
WECALL_WEBHOOK_SECRET = os.environ.get("WECALL_WEBHOOK_SECRET")


class VentanaCerradaError(Exception):
    """El contacto no escribió en las últimas 24h — hace falta una plantilla (409)."""


class EnvioFallidoError(Exception):
    """Meta rechazó el envío (502). No reintentar automáticamente."""
    def __init__(self, detalle):
        self.detalle = detalle
        super().__init__(str(detalle))


def _headers():
    return {"Authorization": f"Bearer {WECALL_API_KEY}"}


def verificar_firma(cuerpo_crudo: bytes, firma_header: str) -> bool:
    """Verifica X-WeCall-Signature: sha256=<hex> contra el cuerpo crudo del POST."""
    if not WECALL_WEBHOOK_SECRET:
        return False
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_recibida = firma_header[len("sha256="):]
    firma_esperada = hmac.new(
        WECALL_WEBHOOK_SECRET.encode("utf-8"), cuerpo_crudo, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(firma_recibida, firma_esperada)


def obtener_contexto(telefono: str, limite: int = 30):
    """Ficha del contacto + últimos mensajes. None si el teléfono no existe en WeCall."""
    r = requests.get(
        f"{WECALL_BASE_URL}/api/v1/contactos/{telefono}/contexto/",
        params={"limite": limite},
        headers=_headers(),
        timeout=10,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def obtener_mensajes_nuevos(telefono: str, desde_id: int):
    """Mensajes con id > desde_id. Para el polling de respaldo."""
    r = requests.get(
        f"{WECALL_BASE_URL}/api/v1/contactos/{telefono}/mensajes/",
        params={"desde_id": desde_id},
        headers=_headers(),
        timeout=10,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json().get("mensajes", [])


def enviar_mensaje(telefono: str, texto: str = None, plantilla: str = None, variables: dict = None):
    """
    Manda un mensaje. Si la ventana de 24h está cerrada, lanza VentanaCerradaError
    (hay que reintentar con plantilla). Si Meta rechaza el envío, lanza EnvioFallidoError.
    """
    body = {"telefono": telefono}
    if plantilla:
        body["plantilla"] = plantilla
        body["variables"] = variables or {}
    else:
        body["texto"] = texto

    r = requests.post(
        f"{WECALL_BASE_URL}/api/v1/mensajes/enviar/",
        json=body,
        headers=_headers(),
        timeout=10,
    )
    if r.status_code == 409:
        raise VentanaCerradaError(f"Ventana cerrada para {telefono}")
    if r.status_code == 502:
        try:
            detalle = r.json().get("error")
        except ValueError:
            detalle = r.text
        raise EnvioFallidoError(detalle)
    r.raise_for_status()
    return r.json()


def generar_enlace_conversacion(telefono: str, minutos_validez: int = 60):
    """URL del chat completo (tipo WhatsApp Web) para el botón 'Ver WhatsApp'."""
    r = requests.post(
        f"{WECALL_BASE_URL}/api/v1/enlaces/conversacion/",
        json={"telefono": telefono, "minutos_validez": minutos_validez},
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()
