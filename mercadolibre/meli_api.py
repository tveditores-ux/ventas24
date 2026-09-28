"""
Cliente mínimo de la API de Mercado Libre: autorización (OAuth), renovación
automática del token y las llamadas que usa este canal.
"""

import os
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from . import meli_db

API = "https://api.mercadolibre.com"
AUTH_URL = os.environ.get("MELI_AUTH_URL", "https://auth.mercadolibre.com.ve/authorization")

APP_ID = os.environ.get("MELI_APP_ID", "")
CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("MELI_REDIRECT_URI", "")

_candado_token = threading.Lock()


class MeliError(Exception):
    def __init__(self, status, detalle):
        self.status = status
        self.detalle = detalle
        super().__init__(f"Mercado Libre respondió {status}: {detalle}")


def url_autorizacion(state):
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })


def _guardar_token(datos):
    expira = datetime.now(timezone.utc) + timedelta(seconds=int(datos.get("expires_in", 21600)))
    meli_db.guardar_credenciales(
        datos["user_id"], datos["access_token"], datos["refresh_token"], expira.isoformat()
    )


def canjear_codigo(code):
    """Primer paso tras autorizar la app en Mercado Libre."""
    r = requests.post(f"{API}/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": APP_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }, headers={"accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        raise MeliError(r.status_code, r.text)
    datos = r.json()
    _guardar_token(datos)
    return datos


def _renovar(cred):
    r = requests.post(f"{API}/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": APP_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": cred["refresh_token"],
    }, headers={"accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        raise MeliError(r.status_code, f"No se pudo renovar el token: {r.text}")
    _guardar_token(r.json())


def token():
    """Devuelve un access_token vigente, renovándolo si vence en menos de 10 min."""
    with _candado_token:
        cred = meli_db.leer_credenciales()
        if not cred or not cred.get("access_token"):
            raise MeliError(401, "La cuenta de Mercado Libre no está conectada. Entrá a /meli/conectar.")
        expira = datetime.fromisoformat(cred["expira_en"])
        if expira - datetime.now(timezone.utc) < timedelta(minutes=10):
            _renovar(cred)
            cred = meli_db.leer_credenciales()
        return cred["access_token"]


def vendedor_id():
    cred = meli_db.leer_credenciales()
    return int(cred["user_id"]) if cred and cred.get("user_id") else None


def conectado():
    cred = meli_db.leer_credenciales()
    return bool(cred and cred.get("access_token"))


def _pedir(metodo, ruta, **kwargs):
    headers = {"Authorization": f"Bearer {token()}"}
    r = requests.request(metodo, f"{API}{ruta}", headers=headers, timeout=20, **kwargs)
    if r.status_code >= 400:
        raise MeliError(r.status_code, r.text[:500])
    return r.json() if r.content else {}


# --- lecturas ---------------------------------------------------------------

def pregunta(question_id):
    return _pedir("GET", f"/questions/{question_id}", params={"api_version": 4})


def preguntas_sin_responder(limite=50):
    return _pedir("GET", "/questions/search", params={
        "seller_id": vendedor_id(), "status": "UNANSWERED", "api_version": 4, "limit": limite,
    }).get("questions", [])


def publicacion(item_id):
    return _pedir("GET", f"/items/{item_id}")


def descripcion(item_id):
    try:
        return _pedir("GET", f"/items/{item_id}/description").get("plain_text", "")
    except MeliError:
        return ""


def buscar_publicaciones(texto, limite=5):
    """Busca entre las publicaciones activas del vendedor."""
    ids = _pedir(
        "GET", f"/users/{vendedor_id()}/items/search",
        params={"q": texto, "status": "active", "limit": limite},
    ).get("results", [])
    if not ids:
        return []
    items = _pedir("GET", "/items", params={
        "ids": ",".join(ids), "attributes": "id,title,price,currency_id,available_quantity,permalink",
    })
    return [i["body"] for i in items if i.get("code") == 200]


def orden(orden_id):
    return _pedir("GET", f"/orders/{orden_id}")


def mensaje(mensaje_id):
    return _pedir("GET", f"/messages/{mensaje_id}", params={"tag": "post_sale"})


def mensajes_pack(pack_id):
    datos = _pedir(
        "GET", f"/messages/packs/{pack_id}/sellers/{vendedor_id()}",
        params={"tag": "post_sale", "mark_as_read": "false", "limit": 50},
    )
    return datos.get("messages", [])


# --- escrituras -------------------------------------------------------------

def responder_pregunta(question_id, texto):
    return _pedir("POST", "/answers", json={"question_id": int(question_id), "text": texto})


def enviar_mensaje(pack_id, comprador_id, texto):
    return _pedir(
        "POST", f"/messages/packs/{pack_id}/sellers/{vendedor_id()}",
        params={"tag": "post_sale"},
        json={
            "from": {"user_id": vendedor_id()},
            "to": {"user_id": int(comprador_id)},
            "text": texto,
        },
    )
