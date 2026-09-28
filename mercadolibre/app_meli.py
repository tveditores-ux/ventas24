"""
Servidor del canal de Mercado Libre (independiente de app_whatsapp.py).

Qué hace:
  1. Responde las preguntas de tus publicaciones.
  2. Cuando alguien compra, le escribe por la mensajería de la orden para
     pasarle los datos de pago y pedirle los datos de envío.
  3. Cuando el comprador informa que pagó, te avisa por WhatsApp para que
     verifiques. Desde el panel confirmas el pago y marcas el envío, y el
     sistema le avisa al comprador en cada paso.

Arranque local:   python -m mercadolibre.app_meli
Producción:       gunicorn mercadolibre.app_meli:app --workers 1 --threads 8
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import traceback

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from flask import Flask, jsonify, redirect, request, send_file  # noqa: E402

from . import agente_meli, avisos, meli_api, meli_db  # noqa: E402

PANEL_TOKEN = os.environ.get("MELI_PANEL_TOKEN", "")
MODO_PREGUNTAS = os.environ.get("MELI_MODO_PREGUNTAS", "auto").lower()    # auto | aprobar
MODO_POSTVENTA = os.environ.get("MELI_MODO_POSTVENTA", "auto").lower()    # auto | aprobar | apagado

app = Flask(__name__)

_candados_pack = {}
_candado_global = threading.Lock()


def _candado(pack_id):
    with _candado_global:
        return _candados_pack.setdefault(str(pack_id), threading.Lock())


def _en_segundo_plano(funcion, *args):
    def envoltura():
        try:
            funcion(*args)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            meli_db.registrar_aviso(f"Error en {funcion.__name__}{args}: {e}", "error")
    threading.Thread(target=envoltura, daemon=True).start()


# ===========================================================================
# Envío de lo que sale hacia Mercado Libre
# ===========================================================================

def enviar_saliente(saliente_id, texto=None):
    s = meli_db.obtener_saliente(saliente_id)
    if not s:
        raise ValueError("No existe ese envío")
    if s["estado"] == "enviado":
        return s
    texto = (texto or s["texto"]).strip()
    try:
        if s["tipo"] == "respuesta":
            meli_api.responder_pregunta(s["referencia"], texto)
        else:
            orden = meli_db.obtener_orden(s["orden_id"])
            resultado = meli_api.enviar_mensaje(s["referencia"], orden["comprador_id"], texto)
            if isinstance(resultado, dict) and resultado.get("id"):
                meli_db.guardar_mensaje(resultado["id"], s["referencia"], "vendedor", texto)
    except meli_api.MeliError as e:
        meli_db.actualizar_saliente(saliente_id, texto=texto, estado="error", error=str(e))
        raise
    meli_db.actualizar_saliente(saliente_id, texto=texto, estado="enviado", error=None, enviado=meli_db.ahora())
    return meli_db.obtener_saliente(saliente_id)


def mandar_al_comprador(orden, texto, cuando="segun_modo", motivo=None):
    """Parte el texto en mensajes de 350 caracteres y los envía o los deja pendientes.

    cuando: "segun_modo" (lo que escribe el bot; respeta MELI_MODO_POSTVENTA),
            "ya" (lo que decides tú desde el panel), "retener" (queda para revisar).
    """
    enviar_ya = cuando == "ya" or (cuando == "segun_modo" and MODO_POSTVENTA == "auto")
    for parte in agente_meli.partir_mensaje(texto):
        sid = meli_db.crear_saliente(
            "mensaje", orden["pack_id"], parte,
            "enviando" if enviar_ya else "pendiente",
            orden_id=orden["id"], motivo=None if enviar_ya else motivo,
        )
        if enviar_ya:
            try:
                enviar_saliente(sid)
            except meli_api.MeliError as e:
                avisos.avisar(f"⚠️ No se pudo enviar un mensaje a {orden['comprador_apodo']} "
                              f"(orden {orden['id']}): {e.detalle}")
                return


# ===========================================================================
# 1) Preguntas
# ===========================================================================

def procesar_pregunta(question_id):
    if not meli_db.marcar_si_nueva(f"pregunta:{question_id}"):
        return  # ya se atendió (Mercado Libre reenvía avisos)
    q = meli_api.pregunta(question_id)
    if q.get("status") != "UNANSWERED":
        return
    item_id = q.get("item_id")
    item = meli_api.publicacion(item_id)
    descripcion = meli_api.descripcion(item_id)
    meli_db.guardar_pregunta(question_id, item_id, item.get("title"), (q.get("from") or {}).get("id"), q.get("text"))

    r = agente_meli.redactar_respuesta(q.get("text", ""), item, descripcion, buscar=meli_api.buscar_publicaciones)

    if MODO_PREGUNTAS == "auto" and not r["necesita_humano"]:
        sid = meli_db.crear_saliente("respuesta", question_id, r["texto"], "enviando")
        try:
            enviar_saliente(sid)
            print(f"  💬 pregunta {question_id} respondida")
        except meli_api.MeliError as e:
            avisos.avisar(f"⚠️ No se pudo publicar la respuesta a una pregunta de «{item.get('title')}»: {e.detalle}")
        return

    motivo = r["motivo"] or "modo aprobar: espera tu visto bueno"
    meli_db.crear_saliente("respuesta", question_id, r["texto"], "pendiente", motivo=motivo)
    avisos.avisar(
        f"❓ Pregunta en «{item.get('title')}»:\n“{q.get('text')}”\n\n"
        f"Respuesta sugerida: {r['texto'] or '(sin sugerencia)'}\n"
        f"Queda pendiente porque: {motivo}"
    )


# ===========================================================================
# 2) Ventas y postventa
# ===========================================================================

def _detalle_orden(o):
    partes = []
    for linea in o.get("order_items", []):
        item = linea.get("item", {})
        partes.append(f"{linea.get('quantity', 1)} x {item.get('title')} ({linea.get('unit_price')} {linea.get('currency_id', '')})")
    return "; ".join(partes)


def procesar_orden(orden_id):
    o = meli_api.orden(orden_id)
    if (o.get("seller") or {}).get("id") not in (None, meli_api.vendedor_id()):
        return
    existente = meli_db.obtener_orden(orden_id)

    if o.get("status") == "cancelled":
        if existente and existente["estado"] != "cancelada":
            meli_db.actualizar_orden(orden_id, estado="cancelada", bot_activo=0)
            avisos.avisar(f"🚫 Se canceló la orden {orden_id} de {existente['comprador_apodo']}.")
        return
    if existente:
        return

    comprador = o.get("buyer") or {}
    nombre = " ".join(filter(None, [comprador.get("first_name"), comprador.get("last_name")])) or None
    pack_id = o.get("pack_id") or o["id"]
    if not meli_db.crear_orden(
        o["id"], pack_id, comprador.get("id"), comprador.get("nickname"), nombre,
        _detalle_orden(o), o.get("total_amount"), o.get("currency_id"),
    ):
        return
    orden = meli_db.obtener_orden(o["id"])
    avisos.avisar(
        f"🛒 ¡Nueva venta en Mercado Libre!\n{orden['detalle']}\n"
        f"Total: {orden['total']} {orden['moneda']}\nComprador: {nombre or ''} ({comprador.get('nickname')})"
    )

    if MODO_POSTVENTA == "apagado":
        return
    with _candado(pack_id):
        texto = agente_meli.conversar_postventa(orden, [], _herramientas_orden(orden["id"]), primer_contacto=True)
        orden = meli_db.obtener_orden(o["id"])
        if orden["estado"] == "nueva":
            meli_db.actualizar_orden(o["id"], estado="esperando_pago")
        _publicar_respuesta_postventa(orden, texto)


def _publicar_respuesta_postventa(orden, texto):
    if not texto:
        return
    problema = agente_meli.revisar_texto(texto)
    texto = agente_meli.insertar_datos_pago(texto)
    if problema:
        mandar_al_comprador(orden, texto, cuando="retener", motivo=f"El filtro de seguridad lo frenó: {problema}")
        avisos.avisar(f"✋ Un mensaje para {orden['comprador_apodo']} quedó retenido ({problema}). Revísalo en el panel.")
    else:
        mandar_al_comprador(orden, texto, motivo="modo aprobar: espera tu visto bueno")


def _herramientas_orden(orden_id):
    def ejecutar(nombre, params):
        orden = meli_db.obtener_orden(orden_id)
        if nombre == "reportar_pago":
            pago_id = meli_db.registrar_pago(orden_id, params)
            meli_db.actualizar_orden(orden_id, estado="pago_reportado")
            avisos.avisar(
                f"💰 {orden['comprador_nombre'] or orden['comprador_apodo']} dice que ya pagó. ¡Verifícalo!\n"
                f"Orden {orden_id}: {orden['detalle']}\nTotal de la orden: {orden['total']} {orden['moneda']}\n"
                f"Método: {params.get('metodo')} {params.get('banco') or ''}\n"
                f"Referencia: {params.get('referencia')}\nMonto: {params.get('monto')}\n"
                f"Fecha: {params.get('fecha') or '-'}  Titular: {params.get('titular') or '-'}\n"
                f"Cuando lo confirmes, márcalo en el panel (pago #{pago_id})."
            )
            return {"ok": True, "mensaje": "Pago registrado. El dueño lo va a verificar; no confirmes el pago al comprador."}
        if nombre == "guardar_datos_envio":
            meli_db.actualizar_orden(orden_id, datos_envio=json.dumps(params, ensure_ascii=False))
            if orden["estado"] == "pago_confirmado":
                avisos.avisar(f"📦 Ya están los datos de envío de la orden {orden_id} ({orden['comprador_apodo']}).")
            return {"ok": True}
        if nombre == "pasar_a_humano":
            meli_db.actualizar_orden(orden_id, bot_activo=0)
            avisos.avisar(f"🙋 La orden {orden_id} ({orden['comprador_apodo']}) necesita que la atiendas tú: "
                          f"{params.get('motivo')}\nEl bot quedó en pausa para esa orden.")
            return {"ok": True}
        return {"error": f"herramienta desconocida {nombre}"}
    return ejecutar


def _pack_de_mensaje(datos):
    msg = datos["messages"][0] if isinstance(datos.get("messages"), list) and datos["messages"] else datos
    for recurso in msg.get("message_resources") or []:
        if recurso.get("name") == "packs":
            return recurso.get("id")
        if recurso.get("name") == "orders":
            orden = meli_db.obtener_orden(recurso.get("id"))
            return orden["pack_id"] if orden else recurso.get("id")
    return None


def procesar_mensaje(mensaje_id):
    pack_id = _pack_de_mensaje(meli_api.mensaje(mensaje_id))
    if pack_id:
        sincronizar_pack(pack_id)


def sincronizar_pack(pack_id):
    orden = meli_db.orden_por_pack(pack_id)
    if not orden:
        return  # orden anterior a este sistema: no se toca
    with _candado(pack_id):
        vendedor = meli_api.vendedor_id()
        mensajes = sorted(meli_api.mensajes_pack(pack_id),
                          key=lambda m: (m.get("message_date") or {}).get("created") or "")
        hay_nuevo_del_comprador = False
        for m in mensajes:
            texto = (m.get("text") or "").strip()
            if not texto:
                continue
            rol = "vendedor" if (m.get("from") or {}).get("user_id") == vendedor else "comprador"
            fecha = (m.get("message_date") or {}).get("created")
            if meli_db.guardar_mensaje(m.get("id"), pack_id, rol, texto, fecha) and rol == "comprador":
                hay_nuevo_del_comprador = True

        orden = meli_db.obtener_orden(orden["id"])
        if not hay_nuevo_del_comprador or not orden["bot_activo"] or orden["estado"] == "cancelada" \
                or MODO_POSTVENTA == "apagado":
            return
        texto = agente_meli.conversar_postventa(orden, meli_db.historial_pack(pack_id), _herramientas_orden(orden["id"]))
        _publicar_respuesta_postventa(meli_db.obtener_orden(orden["id"]), texto)


# ===========================================================================
# Rutas públicas
# ===========================================================================

@app.get("/meli/salud")
def salud():
    return jsonify({"ok": True, "conectado": meli_api.conectado()})


@app.post("/meli/notificaciones")
def notificaciones():
    """Webhook de Mercado Libre. Hay que contestar rápido; el trabajo va aparte.

    Las notificaciones no vienen firmadas, así que nunca se confía en su
    contenido: solo se usa el id del recurso y se lo vuelve a pedir a la API.
    """
    datos = request.get_json(silent=True) or {}
    if meli_api.APP_ID and str(datos.get("application_id")) != str(meli_api.APP_ID):
        return "", 200
    if meli_api.vendedor_id() and str(datos.get("user_id")) != str(meli_api.vendedor_id()):
        return "", 200

    tema = datos.get("topic")
    recurso = str(datos.get("resource") or "")
    identificador = recurso.rstrip("/").split("/")[-1]
    if not identificador:
        return "", 200

    if tema == "questions":
        _en_segundo_plano(procesar_pregunta, identificador)
    elif tema in ("orders_v2", "orders"):
        _en_segundo_plano(procesar_orden, identificador)
    elif tema == "messages":
        _en_segundo_plano(procesar_mensaje, identificador)
    return "", 200


def _firmar(valor):
    return hmac.new(PANEL_TOKEN.encode(), valor.encode(), hashlib.sha256).hexdigest()[:32]


@app.get("/meli/conectar")
def conectar():
    if not PANEL_TOKEN or not hmac.compare_digest(request.args.get("token", ""), PANEL_TOKEN):
        return "Falta ?token=<MELI_PANEL_TOKEN>", 403
    nonce = secrets.token_urlsafe(12)
    return redirect(meli_api.url_autorizacion(f"{nonce}.{_firmar(nonce)}"))


@app.get("/meli/callback")
def callback():
    state = request.args.get("state", "")
    nonce, _, firma = state.partition(".")
    if not PANEL_TOKEN or not hmac.compare_digest(firma, _firmar(nonce)):
        return "Estado inválido. Vuelve a entrar por /meli/conectar.", 400
    try:
        datos = meli_api.canjear_codigo(request.args.get("code", ""))
    except meli_api.MeliError as e:
        return f"No se pudo conectar: {e}", 400
    return (f"<h2>✅ Cuenta de Mercado Libre conectada (usuario {datos['user_id']}).</h2>"
            f"<p>Ya puedes cerrar esta ventana e ir al <a href='/meli/panel'>panel</a>.</p>")


@app.get("/meli/panel")
def panel():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html"))


# ===========================================================================
# API del panel (requiere cabecera X-Panel-Token)
# ===========================================================================

@app.before_request
def proteger_api():
    if request.path.startswith("/meli/api/"):
        if not PANEL_TOKEN or not hmac.compare_digest(request.headers.get("X-Panel-Token", ""), PANEL_TOKEN):
            return jsonify({"error": "no autorizado"}), 401
    return None


def _error(e, codigo=400):
    detalle = e.detalle if isinstance(e, meli_api.MeliError) else str(e)
    return jsonify({"error": detalle}), codigo


@app.get("/meli/api/estado")
def api_estado():
    return jsonify({
        "conectado": meli_api.conectado(),
        "modos": {"preguntas": MODO_PREGUNTAS, "postventa": MODO_POSTVENTA},
        "pendientes": meli_db.salientes_pendientes(),
        "pagos": meli_db.pagos_por_verificar(),
        "ordenes": meli_db.listar_ordenes(),
        "preguntas": meli_db.ultimas_preguntas(),
        "avisos": meli_db.ultimos_avisos(),
        "estados": meli_db.ESTADOS_ORDEN,
    })


@app.post("/meli/api/salientes/<int:sid>/enviar")
def api_enviar(sid):
    try:
        return jsonify(enviar_saliente(sid, (request.get_json(silent=True) or {}).get("texto")))
    except (meli_api.MeliError, ValueError) as e:
        return _error(e)


@app.post("/meli/api/salientes/<int:sid>/descartar")
def api_descartar(sid):
    meli_db.actualizar_saliente(sid, estado="descartado")
    return jsonify({"ok": True})


def _nombre(orden):
    return (orden.get("comprador_nombre") or "").split(" ")[0] or orden.get("comprador_apodo") or ""


@app.post("/meli/api/pagos/<int:pago_id>/confirmar")
def api_confirmar_pago(pago_id):
    pago = meli_db.obtener_pago(pago_id)
    if not pago:
        return _error("No existe ese pago", 404)
    meli_db.actualizar_pago(pago_id, "confirmado")
    meli_db.actualizar_orden(pago["orden_id"], estado="pago_confirmado")
    orden = meli_db.obtener_orden(pago["orden_id"])
    texto = (f"¡Listo, {_nombre(orden)}! Confirmamos tu pago. Ya estamos preparando tu pedido "
             f"y te avisamos por aquí con el número de guía.")
    if not meli_db.datos_envio(orden):
        texto += (" Para despacharlo necesitamos: nombre y apellido, cédula, teléfono de quien recibe, "
                  "ciudad/estado y la agencia u oficina donde retiras.")
    mandar_al_comprador(orden, texto, cuando="ya")
    return jsonify({"ok": True})


@app.post("/meli/api/pagos/<int:pago_id>/rechazar")
def api_rechazar_pago(pago_id):
    pago = meli_db.obtener_pago(pago_id)
    if not pago:
        return _error("No existe ese pago", 404)
    motivo = ((request.get_json(silent=True) or {}).get("motivo") or "").strip()
    meli_db.actualizar_pago(pago_id, "rechazado", motivo)
    meli_db.actualizar_orden(pago["orden_id"], estado="esperando_pago")
    orden = meli_db.obtener_orden(pago["orden_id"])
    texto = "Todavía no logramos ubicar tu pago"
    texto += f" ({motivo})." if motivo else "."
    texto += " ¿Puedes revisar la referencia y el monto, o enviarnos la captura del comprobante, por favor?"
    mandar_al_comprador(orden, texto, cuando="ya")
    return jsonify({"ok": True})


@app.post("/meli/api/ordenes/<orden_id>/enviada")
def api_orden_enviada(orden_id):
    orden = meli_db.obtener_orden(orden_id)
    if not orden:
        return _error("No existe esa orden", 404)
    datos = request.get_json(silent=True) or {}
    guia = (datos.get("guia") or "").strip()
    empresa = (datos.get("empresa") or "").strip()
    meli_db.actualizar_orden(orden_id, estado="enviada", guia=f"{empresa} {guia}".strip())
    texto = f"¡Tu pedido ya va en camino{' por ' + empresa if empresa else ''}!"
    if guia:
        texto += f" Número de guía: {guia}."
    texto += " Gracias por tu compra; cualquier duda nos escribes por aquí."
    mandar_al_comprador(orden, texto, cuando="ya")
    return jsonify({"ok": True})


@app.post("/meli/api/ordenes/<orden_id>/bot")
def api_bot(orden_id):
    activo = 1 if (request.get_json(silent=True) or {}).get("activo") else 0
    meli_db.actualizar_orden(orden_id, bot_activo=activo)
    return jsonify({"ok": True, "bot_activo": activo})


@app.post("/meli/api/ordenes/<orden_id>/mensaje")
def api_mensaje_manual(orden_id):
    orden = meli_db.obtener_orden(orden_id)
    texto = ((request.get_json(silent=True) or {}).get("texto") or "").strip()
    if not orden or not texto:
        return _error("Falta la orden o el texto")
    mandar_al_comprador(orden, texto, cuando="ya")
    return jsonify({"ok": True})


@app.get("/meli/api/ordenes/<orden_id>/conversacion")
def api_conversacion(orden_id):
    orden = meli_db.obtener_orden(orden_id)
    if not orden:
        return _error("No existe esa orden", 404)
    return jsonify({"orden": orden, "mensajes": meli_db.historial_pack(orden["pack_id"])})


@app.post("/meli/api/probar-pregunta")
def api_probar_pregunta():
    """Prueba sin publicar nada: qué respondería el bot a una pregunta sobre una publicación."""
    datos = request.get_json(silent=True) or {}
    try:
        item = meli_api.publicacion(datos.get("item_id", "").strip())
        r = agente_meli.redactar_respuesta(
            datos.get("pregunta", ""), item, meli_api.descripcion(item["id"]),
            buscar=meli_api.buscar_publicaciones,
        )
    except meli_api.MeliError as e:
        return _error(e)
    return jsonify({"publicacion": item.get("title"), **r})


@app.post("/meli/api/sincronizar")
def api_sincronizar():
    """Atiende preguntas sin responder que hayan quedado sin aviso (ej. si el servidor estuvo caído)."""
    try:
        preguntas = meli_api.preguntas_sin_responder()
    except meli_api.MeliError as e:
        return _error(e)
    nuevas = [q["id"] for q in preguntas if not meli_db.obtener_pregunta(q["id"])]
    for qid in nuevas:  # permite reintentar las que quedaron a medias
        with meli_db.conectar() as con:
            con.execute("DELETE FROM notificaciones_vistas WHERE clave = ?", (f"pregunta:{qid}",))
    for qid in nuevas:
        _en_segundo_plano(procesar_pregunta, qid)
    return jsonify({"sin_responder": len(preguntas), "procesando": len(nuevas)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5055)), debug=False)
