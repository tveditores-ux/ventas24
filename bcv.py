"""
Tasa oficial del BCV (USD y EUR en bolívares).

Fuente principal: la página del BCV (bcv.org.ve). Se contrasta con ve.dolarapi.com, que replica la tasa
oficial: si difieren más de 1 % NO se guarda nada y se avisa (nunca se publica una tasa dudosa).
Si el BCV no responde pero la réplica sí, se usa la réplica y queda anotado en la fuente.
"""

import re
import threading
import time

import requests

import db

URL_BCV = "https://www.bcv.org.ve/"
URL_REPLICA = "https://ve.dolarapi.com/v1/{}/oficial"
_UA = {"User-Agent": "Mozilla/5.0 (Ventas24 tasa BCV)"}


def _num(txt):
    return float(txt.strip().replace(".", "").replace(",", ".")) if "," in txt else float(txt)


def _del_bcv() -> dict:
    # El certificado del BCV suele venir con la cadena incompleta; es un dato público y se contrasta con la réplica.
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    h = requests.get(URL_BCV, headers=_UA, timeout=20, verify=False).text
    salida = {}
    for clave, bloque in (("usd", "dolar"), ("eur", "euro")):
        i = h.index(f'id="{bloque}"')
        m = re.search(r"<strong[^>]*>\s*([\d.,]+)\s*</strong>", h[i:i + 1500])
        salida[clave] = _num(m.group(1))
    f = re.search(r'Fecha Valor:\s*<span[^>]*content="(\d{4}-\d{2}-\d{2})', h)
    salida["fecha_valor"] = f.group(1) if f else None
    return salida


def _de_replica() -> dict:
    salida = {}
    for clave, moneda in (("usd", "dolares"), ("eur", "euros")):
        d = requests.get(URL_REPLICA.format(moneda), headers=_UA, timeout=15).json()
        salida[clave] = float(d["promedio"])
        salida["fecha_valor"] = (d.get("fechaActualizacion") or "")[:10] or salida.get("fecha_valor")
    return salida


def actualizar() -> dict:
    """Consulta, contrasta y guarda. Devuelve {ok, usd, eur, fecha_valor, fuente} o {ok: False, error}."""
    bcv, rep, errores = None, None, []
    try:
        bcv = _del_bcv()
    except Exception as e:
        errores.append(f"BCV: {e}")
    try:
        rep = _de_replica()
    except Exception as e:
        errores.append(f"réplica: {e}")
    if not bcv and not rep:
        return {"ok": False, "error": "no pude consultar ninguna fuente (" + "; ".join(errores) + ")"}
    if bcv and rep:
        for k in ("usd", "eur"):
            if abs(bcv[k] - rep[k]) / rep[k] > 0.01:
                return {"ok": False, "error": f"el BCV y la réplica difieren más de 1 % en {k.upper()} "
                                              f"({bcv[k]} vs {rep[k]}): no guardé nada, revísalo a mano"}
    dato, fuente = (bcv, "BCV (automática)") if bcv else (rep, "réplica del BCV (la página del BCV no respondió)")
    if not (dato["usd"] > 0 and dato["eur"] > 0):
        return {"ok": False, "error": "la tasa recibida no es válida"}
    db.guardar_tasa(dato["usd"], fuente, eur=dato["eur"], fecha_valor=dato.get("fecha_valor"))
    db.guardar_config("tasa_auto", "1")
    return {"ok": True, "usd": dato["usd"], "eur": dato["eur"], "fecha_valor": dato.get("fecha_valor"), "fuente": fuente}


def _bucle(horas: float = 3):
    time.sleep(20)
    while True:
        try:
            if db.obtener_config("tasa_auto") != "0":     # "0" = la administración fijó una tasa a mano
                r = actualizar()
                print("  (tasa BCV)", r if not r["ok"] else f"USD {r['usd']} EUR {r['eur']} ({r['fecha_valor']})")
        except Exception as e:
            print(f"  (tasa BCV) error: {e}")
        time.sleep(horas * 3600)


def iniciar():
    threading.Thread(target=_bucle, daemon=True).start()
