"""
Agente de seguridad de pagos.

No decide si un pago es real: eso lo confirma una persona de la
administración mirando el banco. Este módulo revisa cada comprobante
contra el pedido y contra los comprobantes anteriores, y devuelve un
semáforo (verde/amarillo/rojo) con los motivos, para que la persona que
revisa sepa dónde mirar primero. Un comprobante verde tampoco se acepta
solo.

Variables de entorno opcionales:
  CUENTAS_COBRO  fragmentos que identifican TUS cuentas de cobro (últimos
                 dígitos, teléfono de Pago Móvil, correo de Zelle), separados
                 por coma. Si la cuenta destino del comprobante no contiene
                 ninguno, se marca en rojo.
  TASA_BS_USD    bolívares por dólar para comparar comprobantes en Bs.
"""

import os
import re
from datetime import date, datetime, timedelta

import db

TOLERANCIA = 0.01  # 1 % de diferencia aceptada entre monto pagado y total
_ORDEN = {"verde": 0, "amarillo": 1, "rojo": 2}


def normalizar_referencia(ref) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(ref or "").upper())


def _cuentas_cobro():
    return [c.strip().lower() for c in os.environ.get("CUENTAS_COBRO", "").split(",") if c.strip()]


def _tasa():
    try:
        return float(os.environ.get("TASA_BS_USD", ""))
    except ValueError:
        return None


def limpiar_datos(crudo: dict) -> dict:
    """Normaliza lo que llega del formulario; nunca confía en tipos."""
    def txt(k):
        v = (crudo.get(k) or "").strip() if isinstance(crudo.get(k), str) else crudo.get(k)
        return v or None

    try:
        monto = float(str(crudo.get("monto")).replace(",", ".")) if crudo.get("monto") not in (None, "") else None
    except ValueError:
        monto = None
    moneda = (txt("moneda") or "USD").upper()
    if moneda not in ("USD", "VES"):
        moneda = "USD"
    datos = {
        "banco": txt("banco"),
        "referencia": txt("referencia"),
        "monto": monto,
        "moneda": moneda,
        "fecha_pago": txt("fecha_pago"),
        "cuenta_destino": txt("cuenta_destino"),
        "nota": txt("nota"),
    }
    datos["referencia_norm"] = normalizar_referencia(datos["referencia"]) or None
    return datos


def _peor(a, b):
    return a if _ORDEN[a] >= _ORDEN[b] else b


def evaluar(datos: dict, pedido: dict | None, excluir_comprobante_id=None):
    """Devuelve (semaforo, [alertas]). Función de lectura: no escribe en la base."""
    semaforo, alertas = "verde", []

    def alerta(nivel, texto):
        nonlocal semaforo
        semaforo = _peor(semaforo, nivel)
        alertas.append(texto)

    if not pedido:
        alerta("amarillo", "No está atado a ningún pedido.")
    elif pedido.get("estado_pago") == "por_confirmar":
        alerta("amarillo", "El pedido todavía no tiene la existencia confirmada: aún no se le enviaron los datos de pago.")

    # --- referencia -------------------------------------------------
    ref = datos.get("referencia_norm")
    if not ref:
        alerta("amarillo", "Falta el número de referencia.")
    else:
        for comp_id, ped_id, ref_previa in db.referencias_usadas(excluir_comprobante_id):
            coincide = ref == ref_previa or (
                min(len(ref), len(ref_previa)) >= 6 and (ref.endswith(ref_previa) or ref_previa.endswith(ref))
            )
            if not coincide:
                continue
            mismo_pedido = pedido and ped_id == pedido["id"]
            if mismo_pedido:
                alerta("amarillo", f"Referencia ya cargada en este mismo pedido (comprobante #{comp_id}).")
            else:
                alerta("rojo", f"Referencia ya usada en otro comprobante (#{comp_id}). Es la trampa más común.")
            break

    # --- monto ------------------------------------------------------
    monto, moneda = datos.get("monto"), datos.get("moneda") or "USD"
    if monto is None or monto <= 0:
        alerta("amarillo", "Falta el monto o no es válido.")
    elif pedido:
        total = float(pedido.get("total_orden") or pedido["total"] or 0)
        if moneda == "VES":
            tasa = _tasa()
            if tasa:
                monto_usd = monto / tasa
            else:
                monto_usd = None
                alerta("amarillo", "Monto en bolívares y no hay tasa configurada: verifica la conversión a mano.")
        else:
            monto_usd = monto
        if monto_usd is not None and total > 0:
            if monto_usd < total * (1 - TOLERANCIA):
                alerta("rojo", f"Monto menor al pedido: pagó {monto_usd:.2f} USD y el pedido es {total:.2f} USD.")
            elif monto_usd > total * (1 + TOLERANCIA):
                alerta("amarillo", f"Monto mayor al pedido: pagó {monto_usd:.2f} USD y el pedido es {total:.2f} USD.")

    # --- fecha ------------------------------------------------------
    fp = datos.get("fecha_pago")
    if not fp:
        alerta("amarillo", "Falta la fecha del pago.")
    else:
        try:
            dia = datetime.strptime(fp[:10], "%Y-%m-%d").date()
        except ValueError:
            dia = None
            alerta("amarillo", "La fecha del pago no se entiende (usa AAAA-MM-DD).")
        if dia:
            if dia > date.today() + timedelta(days=1):
                alerta("rojo", "La fecha del pago es futura.")
            elif pedido and pedido.get("fecha"):
                try:
                    dia_pedido = datetime.strptime(pedido["fecha"][:10], "%Y-%m-%d").date()
                    if dia < dia_pedido - timedelta(days=1):
                        alerta("amarillo", "El pago es anterior al pedido: puede ser un comprobante reciclado.")
                except ValueError:
                    pass

    # --- cuenta destino ---------------------------------------------
    cuentas = _cuentas_cobro()
    destino = (datos.get("cuenta_destino") or "").lower()
    if not cuentas:
        alerta("amarillo", "No hay cuentas de cobro configuradas: no se pudo comprobar el destino.")
    elif not destino:
        alerta("amarillo", "Falta la cuenta o teléfono destino.")
    elif not any(c in destino for c in cuentas):
        alerta("rojo", "El destino del pago no coincide con ninguna de tus cuentas de cobro.")

    return semaforo, alertas


def registrar_adjunto_whatsapp(contacto_id: int):
    """Un cliente mandó un archivo sin texto. Queda un comprobante pendiente
    sin datos, atado a su pedido por pagar si existe, para que alguien lo
    abra en la conversación y complete los datos. Devuelve (comprobante_id, pedido)."""
    pedido = db.pedido_por_pagar_reciente(contacto_id)
    alertas = ["Adjunto recibido por WhatsApp: abre la conversación, mira la imagen y completa los datos."]
    if not pedido:
        alertas.append("El cliente no tiene un pedido por pagar: puede no ser un comprobante.")
    elif pedido["estado_pago"] == "por_confirmar":
        alertas.append("El pedido todavía no tiene la existencia confirmada: no se le han enviado los datos de pago.")
    comp_id = db.crear_comprobante(
        contacto_id, pedido["id"] if pedido else None, "whatsapp", {}, "amarillo", alertas
    )
    return comp_id, pedido
