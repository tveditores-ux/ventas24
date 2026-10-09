"""
Ciclo de venta de punta a punta. Cada paso lo ejecuta una persona del equipo
desde el CRM y el SISTEMA avisa al cliente con un texto fijo (no un modelo),
para que nunca falte el aviso ni se invente una condición.

    pedido registrado por el bot  ──► por_confirmar   (nadie ha verificado existencia)
    equipo confirma existencia    ──► por_pagar       (cliente recibe total y datos de pago)
    cliente envía comprobante     ──► comprobante_recibido (agente de seguridad lo revisa)
    administración confirma pago  ──► pago_confirmado (cliente recibe confirmación)
    equipo despacha               ──► entregado       (cliente recibe aviso de salida)

Si algo no se puede cumplir (sin existencia, comprobante rechazado) el cliente
también recibe aviso. Si el aviso no sale (ventana de 24 h cerrada), la función
lo reporta para que la persona escriba desde WeCall.

Variable de entorno DATOS_PAGO: texto con las cuentas reales de cobro (Pago Móvil,
Zelle…) que se envía al cliente cuando se confirma la existencia. Si no está, el
cliente recibe un aviso de que la administración le escribirá los datos y se abre
una solicitud para el equipo.
"""

import os

import db
import eventos
import wecall
from wecall import EnvioFallidoError, VentanaCerradaError


def _contacto(contacto_id: int):
    with db.conectar() as con:
        fila = con.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
    return dict(fila) if fila else None


def notificar_cliente(contacto_id: int, texto: str):
    """Manda un mensaje al cliente por WeCall. Devuelve (ok, motivo)."""
    contacto = _contacto(contacto_id)
    if not contacto:
        return False, "contacto no encontrado"
    try:
        wecall.enviar_mensaje(contacto["telefono"], texto=texto)
    except VentanaCerradaError:
        eventos.registrar("aviso_cliente", contacto["telefono"], "ventana de 24 h cerrada", ok=False)
        return False, "la ventana de 24 h está cerrada: escríbele desde WeCall"
    except EnvioFallidoError as e:
        eventos.registrar("aviso_cliente", contacto["telefono"], f"Meta rechazó: {e.detalle}", ok=False)
        return False, f"WhatsApp rechazó el envío ({e.detalle})"
    except Exception as e:
        eventos.registrar("aviso_cliente", contacto["telefono"], str(e), ok=False)
        return False, f"no se pudo enviar ({e})"
    db.registrar_interaccion(contacto_id, "assistant", texto)
    eventos.registrar("aviso_cliente", contacto["telefono"], texto[:80])
    return True, "enviado"


def _nombre(contacto):
    n = (contacto.get("nombre") or "").strip().split()
    return n[0] if n else ""


def _saludo(contacto):
    n = _nombre(contacto)
    return f"{n}, " if n else ""


def _resumen(lineas):
    activas = [l for l in lineas if l["estado"] != "cancelado"]
    partes = [f"• {l['cantidad']} x {l['producto'][:70]}: ${l['total']:.2f}" for l in activas]
    total = round(sum(l["total"] or 0 for l in activas), 2)
    return "\n".join(partes), total


def datos_pago() -> str:
    """Cuentas de cobro: variable de entorno DATOS_PAGO o, si no está, la configuración guardada en el CRM."""
    return (os.environ.get("DATOS_PAGO", "").strip() or db.obtener_config("datos_pago") or "").strip()


def texto_existencia_confirmada(pedido_id: int) -> str:
    """Mensaje al cliente cuando la existencia de su orden está confirmada: total final y datos de pago.
    Si no hay datos de pago configurados, avisa que la administración los envía y abre una solicitud."""
    lineas = db.lineas_de_orden(pedido_id)
    contacto = _contacto(lineas[0]["contacto_id"])
    resumen, total = _resumen(lineas)
    pago = datos_pago()
    if pago:
        return (
            f"{_saludo(contacto)}confirmamos la existencia de tu pedido:\n{resumen}\n\nTotal: ${total:.2f}\n\n"
            f"Datos para el pago:\n{pago}\n\n"
            "Cuando pagues, envíame por aquí la captura del comprobante y la validamos."
        )
    db.crear_solicitud(
        contacto["id"], "pago", f"Enviar datos de pago a {contacto.get('nombre') or contacto['telefono']}",
        f"Existencia confirmada. Total ${total:.2f}.\n{resumen}",
    )
    return (
        f"{_saludo(contacto)}confirmamos la existencia de tu pedido:\n{resumen}\n\nTotal: ${total:.2f}\n\n"
        "En un momento una persona de la administración te escribe por aquí con los datos para el pago."
    )


def confirmar_existencia(pedido_id: int, quien: str = "equipo"):
    """La persona verificó que hay existencia. Pasa la orden a 'por_pagar' y avisa al cliente."""
    lineas = db.lineas_de_orden(pedido_id)
    if not any(l["estado_pago"] == "por_confirmar" and l["estado"] != "cancelado" for l in lineas):
        return {"ok": False, "error": "esta orden no está esperando confirmación de existencia"}
    db.cambiar_estado_pago_orden(pedido_id, ("por_confirmar",), "por_pagar")
    db.confirmar_existencia_por(pedido_id, quien)
    contacto_id = lineas[0]["contacto_id"]
    texto = texto_existencia_confirmada(pedido_id)
    ok, motivo = notificar_cliente(contacto_id, texto)
    return {"ok": True, "aviso": motivo if not ok else "enviado", "aviso_ok": ok, "datos_pago_enviados": bool(datos_pago())}


def sin_existencia(pedido_id: int, nota: str | None = None):
    """No hay existencia: cancela la orden, anota al cliente en la lista de espera y le avisa."""
    lineas = db.lineas_de_orden(pedido_id)
    if not lineas:
        return {"ok": False, "error": "pedido no encontrado"}
    if any(l["estado_pago"] == "pago_confirmado" for l in lineas):
        return {"ok": False, "error": "el pago ya fue confirmado: no se puede cancelar por existencia"}
    contacto = _contacto(lineas[0]["contacto_id"])
    resumen, _ = _resumen(lineas)
    for l in lineas:
        if l["estado"] != "cancelado":
            db.registrar_espera(contacto["id"], l["producto"])
    db.cancelar_orden(pedido_id)
    texto = (
        f"{_saludo(contacto)}revisamos tu pedido y por ahora no tenemos existencia de:\n{resumen}\n\n"
        "Te dejamos anotado en la lista de espera y te avisamos apenas lo repongamos."
    )
    if nota:
        texto += f"\n\n{nota}"
    ok, motivo = notificar_cliente(contacto["id"], texto)
    return {"ok": True, "aviso": motivo if not ok else "enviado", "aviso_ok": ok}


def aviso_pago_confirmado(comprobante_id: int):
    comp = db.obtener_comprobante(comprobante_id)
    contacto = _contacto(comp["contacto_id"])
    texto = (
        f"{_saludo(contacto)}recibimos y validamos tu pago. Tu pedido pasa a preparación "
        "y te avisamos por aquí en cuanto salga."
    )
    ok, motivo = notificar_cliente(contacto["id"], texto)
    return {"aviso": motivo if not ok else "enviado", "aviso_ok": ok}


def aviso_pago_rechazado(comprobante_id: int):
    comp = db.obtener_comprobante(comprobante_id)
    contacto = _contacto(comp["contacto_id"])
    texto = (
        f"{_saludo(contacto)}no pudimos validar el comprobante que enviaste. "
        "¿Puedes revisar que la captura se vea completa y decirme la referencia del pago? "
        "Si ya lo hiciste bien, una persona del equipo lo revisa contigo."
    )
    ok, motivo = notificar_cliente(contacto["id"], texto)
    return {"aviso": motivo if not ok else "enviado", "aviso_ok": ok}


def aviso_despacho(pedido_id: int, nota: str | None = None):
    lineas = db.lineas_de_orden(pedido_id)
    contacto = _contacto(lineas[0]["contacto_id"])
    resumen, total = _resumen(lineas)
    texto = f"{_saludo(contacto)}tu pedido ya fue despachado:\n{resumen}"
    if nota:
        texto += f"\n\n{nota}"
    ok, motivo = notificar_cliente(contacto["id"], texto)
    return {"aviso": motivo if not ok else "enviado", "aviso_ok": ok}
