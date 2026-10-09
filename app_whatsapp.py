"""
Servidor web que conecta el Agente a WhatsApp.

Dos canales:
  - Twilio (sandbox/número propio) — ver /whatsapp. Se deja andando como
    respaldo; WeCall Inbox es el canal real del negocio.
  - WeCall Inbox (WhatsApp Business real vía Meta Cloud API) — ver
    /webhook/wecall. Además de recibir el webhook, hay un hilo de
    polling de respaldo (revisa mensajes nuevos cada cierto intervalo)
    porque el webhook de WeCall es "best-effort" sin reintentos.

Uso:
    python app_whatsapp.py

Requiere en .env: ANTHROPIC_API_KEY, y según el canal, TWILIO_ACCOUNT_SID/
TWILIO_AUTH_TOKEN/TWILIO_WHATSAPP_FROM y/o WECALL_API_KEY/WECALL_WEBHOOK_SECRET.

El webhook responde de inmediato (TwiML vacío o 200 vacío según el canal)
y procesa el mensaje del agente en un hilo aparte, mandando la respuesta
real por la API correspondiente cuando esté lista. Esto evita que el
webhook corte la conexión si el agente (que hace varias llamadas a la
API de Anthropic) tarda más de lo que el remitente espera por una
respuesta síncrona.
"""

import hmac
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify, send_from_directory, session
from twilio.rest import Client
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

import db
import ciclo
import eventos
import seguridad
import wecall
from wecall import VentanaCerradaError, EnvioFallidoError
from perfiles import Agente

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("CRM_DASHBOARD_TOKEN") or "clave-insegura-de-desarrollo"
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

CRM_DASHBOARD_TOKEN = os.environ.get("CRM_DASHBOARD_TOKEN")


def _token_valido():
    """Para los endpoints /debug/* de mantenimiento — no para el dashboard del cliente."""
    if not CRM_DASHBOARD_TOKEN:
        return False
    recibido = request.headers.get("Authorization", "")
    if recibido.startswith("Bearer "):
        recibido = recibido[len("Bearer "):]
    else:
        recibido = request.args.get("token", "")
    return hmac.compare_digest(recibido, CRM_DASHBOARD_TOKEN)


def _usuario_actual():
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return None
    usuario = db.obtener_usuario(usuario_id)
    if not usuario or not usuario["activo"]:
        return None
    return usuario


def requiere_sesion(roles=None):
    """Decorador para las rutas /api/crm/*: exige sesión iniciada y,
    opcionalmente, que el rol del usuario esté entre los permitidos."""
    def decorador(f):
        @wraps(f)
        def envoltura(*args, **kwargs):
            usuario = _usuario_actual()
            if not usuario:
                return jsonify({"error": "no autenticado"}), 401
            if roles and usuario["rol"] not in roles:
                return jsonify({"error": "no autorizado para tu rol"}), 403
            request.usuario = usuario
            return f(*args, **kwargs)
        return envoltura
    return decorador


@app.after_request
def _cors(resp):
    if request.path.startswith("/api/crm/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/crm/<path:_>", methods=["OPTIONS"])
def _crm_preflight(_):
    return ("", 204)


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "login.html")


@app.route("/api/crm/login", methods=["POST"])
def api_login():
    datos = request.get_json(silent=True) or {}
    email = (datos.get("email") or "").strip().lower()
    password = datos.get("password") or ""
    usuario = db.obtener_usuario_por_email(email)
    if not usuario or not check_password_hash(usuario["password_hash"], password):
        return jsonify({"error": "email o contraseña incorrectos"}), 401
    session.clear()
    session["usuario_id"] = usuario["id"]
    session.permanent = True
    return jsonify({"id": usuario["id"], "nombre": usuario["nombre"], "rol": usuario["rol"]})


@app.route("/api/crm/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/crm/yo", methods=["GET"])
def api_yo():
    usuario = _usuario_actual()
    if not usuario:
        return jsonify({"error": "no autenticado"}), 401
    return jsonify({"id": usuario["id"], "nombre": usuario["nombre"], "rol": usuario["rol"], "email": usuario["email"]})


@app.route("/debug/crear-usuario", methods=["POST"])
def debug_crear_usuario():
    """ADMIN — crea un usuario del dashboard (super_admin/admin/vendedor).
    Protegido con CRM_DASHBOARD_TOKEN porque todavía no hay un super_admin
    que pueda crear el primero desde la interfaz."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    datos = request.get_json(silent=True) or {}
    nombre = (datos.get("nombre") or "").strip()
    email = (datos.get("email") or "").strip().lower()
    password = datos.get("password") or ""
    rol = datos.get("rol") or "vendedor"
    if not nombre or not email or len(password) < 8:
        return jsonify({"error": "faltan nombre/email, o la contraseña tiene menos de 8 caracteres"}), 400
    if rol not in db.ROLES_VALIDOS:
        return jsonify({"error": f"rol inválido, usa uno de {db.ROLES_VALIDOS}"}), 400
    try:
        usuario_id = db.crear_usuario(nombre, email, generate_password_hash(password), rol)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": usuario_id, "nombre": nombre, "email": email, "rol": rol})


@app.route("/crm", methods=["GET"])
def crm_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "crm.html")


@app.route("/monitor", methods=["GET"])
def monitor_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "monitor.html")


@app.route("/hub", methods=["GET"])
def hub_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "hub.html")


@app.route("/api/crm/eventos", methods=["GET"])
@requiere_sesion(roles=["super_admin"])
def crm_eventos():
    desde_id = request.args.get("desde_id", default=0, type=int)
    return jsonify(eventos.listar(desde_id))


@app.route("/api/crm/contactos", methods=["GET"])
@requiere_sesion()
def crm_contactos():
    return jsonify(db.listar_contactos_con_actividad(usuario=request.usuario))


@app.route("/api/crm/catalogo", methods=["GET"])
@requiere_sesion()
def crm_catalogo():
    return jsonify(db.listar_catalogo_completo())


@app.route("/api/crm/espera", methods=["GET"])
@requiere_sesion()
def crm_espera():
    return jsonify(db.listar_espera(solo_pendientes=True))


@app.route("/api/crm/pedidos", methods=["GET"])
@requiere_sesion()
def crm_pedidos():
    return jsonify(db.listar_pedidos(usuario=request.usuario))


ROLES_ADMIN = ["super_admin", "admin"]


def _puede_ver_pedido(usuario, pedido):
    return usuario["rol"] != "vendedor" or pedido.get("asignado_a") in (None, usuario["id"])


@app.route("/api/crm/comprobantes", methods=["GET"])
@requiere_sesion()
def crm_comprobantes():
    return jsonify(db.listar_comprobantes(usuario=request.usuario))


@app.route("/api/crm/pedidos/<int:pedido_id>/comprobante", methods=["POST"])
@requiere_sesion()
def crm_cargar_comprobante(pedido_id):
    """Carga los datos de un comprobante a mano y los pasa por el agente de seguridad.
    Con `comprobante_id` completa uno que llegó por WhatsApp sin datos."""
    pedido = db.obtener_pedido(pedido_id)
    if not pedido:
        return jsonify({"error": "pedido no encontrado"}), 404
    if not _puede_ver_pedido(request.usuario, pedido):
        return jsonify({"error": "este pedido es de otro vendedor"}), 403
    if pedido["estado_pago"] == "pago_confirmado":
        return jsonify({"error": "el pago de este pedido ya está confirmado"}), 409

    crudo = request.get_json(silent=True) or {}
    datos = seguridad.limpiar_datos(crudo)
    comp_id = crudo.get("comprobante_id")
    if comp_id:
        existente = db.obtener_comprobante(int(comp_id))
        if not existente or existente["estado"] != "pendiente" or existente["contacto_id"] != pedido["contacto_id"]:
            return jsonify({"error": "comprobante no válido para este pedido"}), 400
    semaforo, alertas = seguridad.evaluar(datos, pedido, excluir_comprobante_id=comp_id)
    if comp_id:
        db.actualizar_datos_comprobante(int(comp_id), datos, semaforo, alertas)
    else:
        comp_id = db.crear_comprobante(
            pedido["contacto_id"], pedido_id, "manual", datos, semaforo, alertas, request.usuario["id"]
        )
    eventos.registrar("comprobante", pedido["telefono"], f"#{comp_id} {semaforo}: {'; '.join(alertas) or 'sin alertas'}",
                      ok=semaforo != "rojo")
    return jsonify({"id": comp_id, "semaforo": semaforo, "alertas": alertas})


@app.route("/api/crm/comprobantes/<int:comprobante_id>/revisar", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def crm_revisar_comprobante(comprobante_id):
    """Solo la administración confirma o rechaza. Confirmar un comprobante en
    rojo o sin datos exige una nota que explique por qué."""
    datos = request.get_json(silent=True) or {}
    decision = datos.get("decision")
    nota = (datos.get("nota") or "").strip() or None
    if decision not in ("confirmar", "rechazar"):
        return jsonify({"error": "decision debe ser 'confirmar' o 'rechazar'"}), 400
    comp = db.obtener_comprobante(comprobante_id)
    if not comp:
        return jsonify({"error": "comprobante no encontrado"}), 404
    if decision == "confirmar":
        if not comp["pedido_id"]:
            return jsonify({"error": "este comprobante no está atado a un pedido"}), 400
        if (comp["semaforo"] == "rojo" or not comp["referencia_norm"]) and not nota:
            return jsonify({"error": "este comprobante tiene alertas graves: escribe una nota que explique por qué lo confirmas"}), 400
    ok, motivo = db.revisar_comprobante(comprobante_id, request.usuario["id"], decision == "confirmar", nota)
    if not ok:
        return jsonify({"error": motivo}), 409
    eventos.registrar("pago_revisado", comp["telefono"],
                      f"comprobante #{comprobante_id} {decision} por {request.usuario['nombre']}")
    aviso = ciclo.aviso_pago_confirmado(comprobante_id) if decision == "confirmar" else ciclo.aviso_pago_rechazado(comprobante_id)
    return jsonify({"ok": True, **aviso})


@app.route("/api/crm/pedidos/<int:pedido_id>/despachar", methods=["POST"])
@requiere_sesion()
def crm_despachar_pedido(pedido_id):
    pedido = db.obtener_pedido(pedido_id)
    if not pedido:
        return jsonify({"error": "pedido no encontrado"}), 404
    if not _puede_ver_pedido(request.usuario, pedido):
        return jsonify({"error": "este pedido es de otro vendedor"}), 403
    nota = ((request.get_json(silent=True) or {}).get("nota") or "").strip() or None
    ok, motivo = db.despachar_pedido(pedido_id, request.usuario["id"], nota)
    if not ok:
        eventos.registrar("despacho_bloqueado", pedido["telefono"], f"pedido {pedido_id}: {motivo}", ok=False)
        return jsonify({"error": motivo}), 409
    eventos.registrar("despacho", pedido["telefono"], f"pedido {pedido_id} despachado por {request.usuario['nombre']}")
    return jsonify({"ok": True, **ciclo.aviso_despacho(pedido_id, nota)})


@app.route("/api/crm/pedidos/<int:pedido_id>/existencia", methods=["POST"])
@requiere_sesion()
def crm_existencia(pedido_id):
    """Paso 'existencia': la persona verificó el inventario. 'confirmar' pasa la orden a por_pagar
    y le manda el total y los datos de pago al cliente; 'sin_existencia' la cancela y lo anota en espera."""
    pedido = db.obtener_pedido(pedido_id)
    if not pedido:
        return jsonify({"error": "pedido no encontrado"}), 404
    if not _puede_ver_pedido(request.usuario, pedido):
        return jsonify({"error": "este pedido es de otro vendedor"}), 403
    datos = request.get_json(silent=True) or {}
    decision = datos.get("decision")
    if decision == "confirmar":
        res = ciclo.confirmar_existencia(pedido_id, request.usuario["nombre"])
    elif decision == "sin_existencia":
        res = ciclo.sin_existencia(pedido_id, (datos.get("nota") or "").strip() or None)
    else:
        return jsonify({"error": "decision debe ser 'confirmar' o 'sin_existencia'"}), 400
    if not res.get("ok"):
        return jsonify({"error": res.get("error", "no se pudo")}), 409
    eventos.registrar("existencia", pedido["telefono"], f"pedido {pedido_id}: {decision} por {request.usuario['nombre']}")
    return jsonify(res)


@app.route("/api/crm/pendientes", methods=["GET"])
@requiere_sesion()
def crm_pendientes():
    usuario = request.usuario
    return jsonify({
        "conteos": db.contar_pendientes(usuario),
        "solicitudes": db.listar_solicitudes(usuario),
        "derivadas": db.listar_derivadas(usuario),
    })


@app.route("/api/crm/solicitudes/<int:solicitud_id>/resolver", methods=["POST"])
@requiere_sesion()
def crm_resolver_solicitud(solicitud_id):
    if not db.resolver_solicitud(solicitud_id, request.usuario["id"]):
        return jsonify({"error": "la solicitud no existe o ya estaba resuelta"}), 409
    return jsonify({"ok": True})


@app.route("/api/crm/conversacion/<int:contacto_id>", methods=["GET"])
@requiere_sesion()
def crm_conversacion(contacto_id):
    with db.conectar() as con:
        fila = con.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
    if not fila:
        return jsonify({"error": "contacto no encontrado"}), 404
    contacto = dict(fila)

    if request.usuario["rol"] == "vendedor" and contacto.get("asignado_a") not in (None, request.usuario["id"]):
        return jsonify({"error": "esta conversación está asignada a otro vendedor"}), 403

    mensajes = None
    if contacto.get("wecall_ultimo_id") is not None:
        try:
            contexto = wecall.obtener_contexto(contacto["telefono"], limite=100)
            if contexto:
                mensajes = contexto.get("mensajes", [])
        except Exception as e:
            print(f"  (crm) no pude traer contexto de WeCall para conversación: {e}")

    if mensajes is None:
        # Contacto sin WeCall (ej. solo Twilio): se arma con lo que hay en crm.db.
        mensajes = [
            {"direccion": "entrante" if h["role"] == "user" else "saliente", "texto": h["content"]}
            for h in db.obtener_historial(contacto_id)
        ]

    return jsonify({"contacto": contacto, "mensajes": mensajes})


@app.route("/api/crm/modo-manual/<int:contacto_id>", methods=["POST"])
@requiere_sesion()
def crm_modo_manual(contacto_id):
    usuario = request.usuario
    datos = request.get_json(silent=True) or {}
    activo = 1 if datos.get("activo") else 0

    if usuario["rol"] == "vendedor" and activo:
        # Activar modo manual es "atender" la conversación — si estaba
        # libre, se la queda; si ya era de otro vendedor, no puede tocarla.
        if not db.asignar_conversacion_si_libre(contacto_id, usuario["id"]):
            return jsonify({"error": "esta conversación ya está asignada a otro vendedor"}), 403

    db.actualizar_contacto(contacto_id, modo_manual=activo)
    with db.conectar() as con:
        fila = con.execute("SELECT asignado_a FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
    return jsonify({"contacto_id": contacto_id, "modo_manual": bool(activo), "asignado_a": fila["asignado_a"] if fila else None})


@app.route("/api/crm/enviar/<int:contacto_id>", methods=["POST"])
@requiere_sesion()
def crm_enviar(contacto_id):
    """Manda una respuesta manual desde el CRM — un humano toma la
    conversación: la marca en modo manual y (si era de un vendedor
    libre) se la asigna a quien escribe, igual que el toggle."""
    usuario = request.usuario
    datos = request.get_json(silent=True) or {}
    texto = (datos.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "falta el texto"}), 400

    with db.conectar() as con:
        fila = con.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
    if not fila:
        return jsonify({"error": "contacto no encontrado"}), 404
    contacto = dict(fila)

    if usuario["rol"] == "vendedor":
        if not db.asignar_conversacion_si_libre(contacto_id, usuario["id"]):
            return jsonify({"error": "esta conversación ya está asignada a otro vendedor"}), 403

    try:
        wecall.enviar_mensaje(contacto["telefono"], texto=texto)
    except VentanaCerradaError:
        return jsonify({"error": "la ventana de 24h está cerrada — hace falta una plantilla aprobada"}), 409
    except EnvioFallidoError as e:
        return jsonify({"error": f"Meta rechazó el envío: {e.detalle}"}), 502

    db.actualizar_contacto(contacto_id, modo_manual=1)
    db.registrar_interaccion(contacto_id, "assistant", texto)
    eventos.registrar("envio_wecall", contacto["telefono"], f"manual por {usuario['nombre']}: {texto[:60]}")
    return jsonify({"ok": True})


@app.route("/debug/cargar-catalogo", methods=["POST"])
def debug_cargar_catalogo():
    """DIAGNÓSTICO / ADMIN — recarga el catálogo desde
    catalogo_50_modelos_venezuela.csv (que viaja con el deploy) hacia la
    crm.db real de producción, reemplazando lo que haya. Protegido con
    el mismo token del dashboard porque reescribe datos reales."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    import migrar_datos
    with db.conectar() as con:
        antes = con.execute("SELECT COUNT(*) AS n FROM catalogo").fetchone()["n"]
    migrar_datos.migrar_catalogo(forzar=True)
    with db.conectar() as con:
        despues = con.execute("SELECT COUNT(*) AS n FROM catalogo").fetchone()["n"]
    return jsonify({"productos_antes": antes, "productos_despues": despues})


@app.route("/debug/salomon-bitacora", methods=["GET"])
def debug_salomon_bitacora():
    """DIAGNÓSTICO — últimas objeciones del vigilante (por qué Salomón corrigió o derivó a una persona)."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    with db.conectar() as con:
        try:
            filas = con.execute(
                "SELECT b.id, b.fecha, c.telefono, b.capa, b.motivo, b.borrador, b.accion FROM salomon_bitacora b "
                "LEFT JOIN contactos c ON c.id = b.contacto_id ORDER BY b.id DESC LIMIT 20"
            ).fetchall()
        except Exception:
            filas = []
    return jsonify([dict(f) for f in filas])


@app.route("/debug/modo-manual/<telefono>", methods=["POST"])
def debug_modo_manual(telefono):
    """ADMIN — pone o quita el modo manual de un contacto (?valor=0|1). Quitarlo devuelve la conversación al bot."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    valor = request.args.get("valor", type=int)
    if valor not in (0, 1):
        return jsonify({"error": "usa ?valor=0 o ?valor=1"}), 400
    telefono = db.normalizar_telefono(telefono)
    contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
    db.actualizar_contacto(contacto_id, modo_manual=valor)
    return jsonify({"telefono": telefono, "contacto_id": contacto_id, "modo_manual": valor})


@app.route("/debug/datos-prueba", methods=["POST"])
def debug_datos_prueba():
    """ADMIN — carga o quita datos FICTICIOS de prueba: inventario confirmado en algunos productos y
    datos de pago de ejemplo. ?accion=cargar | quitar. Todo es reversible y queda marcado como prueba."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    accion = request.args.get("accion")
    if accion == "cargar":
        consultas = ["filtro aceite aveo", "filtro aceite corolla", "filtro aire corolla", "bujia ngk",
                     "correa tiempo hilux", "pastilla freno", "amortiguador", "bomba agua"]
        afectados = db.cargar_stock_prueba(consultas, [40, 12, 5, 0])
        db.guardar_config(
            "datos_pago",
            "PRUEBA (datos ficticios, no pagues):\n"
            "Pago Móvil: Banco Ejemplo (0000) · Tel 0414-0000000 · C.I. V-00000000\n"
            "Zelle: pagos@ejemplo.com",
        )
        return jsonify({"productos_con_inventario_de_prueba": len(afectados), "muestra": afectados[:12],
                        "datos_pago": "ficticios guardados"})
    if accion == "todo":
        cantidad = request.args.get("cantidad", default=100, type=int)
        with db.conectar() as con:
            n = con.execute(
                "UPDATE catalogo SET stock = ?, stock_verificado = 1, stock_prueba = 1", (cantidad,)
            ).rowcount
        return jsonify({"productos_actualizados": n, "stock_de_cada_uno": cantidad,
                        "nota": "cantidades de muestra; accion=quitar las devuelve a 'por confirmar'"})
    if accion == "quitar":
        quitados = db.quitar_stock_prueba()
        pago = db.obtener_config("datos_pago") or ""
        if pago.startswith("PRUEBA"):
            db.guardar_config("datos_pago", None)
        return jsonify({"productos_restablecidos": quitados})
    return jsonify({"error": "usa ?accion=cargar, todo o quitar"}), 400


@app.route("/debug/cargar-lista", methods=["POST"])
def debug_cargar_lista():
    """ADMIN — reemplaza el catálogo por una lista de precios enviada como CSV en el
    cuerpo (columnas: codigo,descripcion,marca,precio_full,precio_mayor). Respalda el
    catálogo anterior en la tabla catalogo_respaldo. Exige ?reemplazar=1 y el token.
    La existencia queda 'por confirmar'; ?stock=N fija la cantidad interna (def. 999)."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    if request.args.get("reemplazar") != "1":
        return jsonify({"error": "esto reemplaza todo el catálogo: agrega ?reemplazar=1"}), 400
    import csv
    import io
    texto = request.get_data().decode("utf-8-sig", errors="replace")
    filas, descartadas = [], 0
    for r in csv.DictReader(io.StringIO(texto)):
        try:
            precio = float(r["precio_full"])
            mayor = float(r["precio_mayor"]) if r.get("precio_mayor") else None
        except (KeyError, ValueError):
            descartadas += 1
            continue
        nombre, codigo = (r.get("descripcion") or "").strip(), (r.get("codigo") or "").strip()
        if not nombre or not codigo or precio <= 0:
            descartadas += 1
            continue
        filas.append({"codigo": codigo, "nombre": nombre, "marca": (r.get("marca") or "").strip(),
                      "precio": precio, "precio_mayor": mayor})
    if len(filas) < 100:
        return jsonify({"error": f"solo {len(filas)} filas válidas; no reemplazo el catálogo", "descartadas": descartadas}), 400
    cargadas = db.reemplazar_catalogo(filas, stock=request.args.get("stock", default=999, type=int))
    return jsonify({"cargadas": cargadas, "descartadas": descartadas, "respaldo": "tabla catalogo_respaldo"})


@app.route("/debug/version", methods=["GET"])
def debug_version():
    return jsonify({"commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "desconocido")})


@app.route("/debug/wecall-key-check", methods=["POST"])
def debug_wecall_key_check():
    """DIAGNÓSTICO TEMPORAL — nunca expone las claves reales. Solo dice si
    el valor que mandaste coincide EXACTO con lo que el proceso tiene
    cargado, para descartar espacios/saltos de línea extra sin filtrar
    el secreto por HTTP."""
    import hmac as _hmac
    datos = request.get_json(silent=True) or {}
    real_key = os.environ.get("WECALL_API_KEY") or ""
    real_secret = os.environ.get("WECALL_WEBHOOK_SECRET") or ""
    resultado = {}
    if "api_key" in datos:
        candidato = datos["api_key"]
        resultado["api_key_coincide"] = _hmac.compare_digest(candidato, real_key)
        resultado["api_key_longitud_real"] = len(real_key)
        resultado["api_key_longitud_recibida"] = len(candidato)
    if "webhook_secret" in datos:
        candidato = datos["webhook_secret"]
        resultado["webhook_secret_coincide"] = _hmac.compare_digest(candidato, real_secret)
        resultado["webhook_secret_longitud_real"] = len(real_secret)
        resultado["webhook_secret_longitud_recibida"] = len(candidato)
    return jsonify(resultado)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
    else None
)

# Serializa el chequeo "¿ya procesé este mensaje de WeCall?" entre el
# webhook y el polling de respaldo, para no mandar la respuesta dos veces.
wecall_lock = threading.Lock()


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


def _mapear_historial_wecall(mensajes, excluir_id=None):
    """Convierte los mensajes de WeCall al formato {role, content} que usa el agente."""
    historial = []
    for m in mensajes:
        if excluir_id is not None and m.get("id") == excluir_id:
            continue
        rol = "user" if m.get("direccion") == "entrante" else "assistant"
        texto = m.get("texto") or "[archivo adjunto]"
        historial.append({"role": rol, "content": texto})
    return historial


def procesar_mensaje_wecall(telefono: str, mensaje: dict):
    """Procesa un mensaje entrante de WeCall (llamado desde el webhook o el polling)."""
    mensaje_id = mensaje.get("id")
    texto_mensaje = (mensaje.get("texto") or "").strip()
    if mensaje.get("direccion") != "entrante" or mensaje_id is None:
        return

    telefono = db.normalizar_telefono(telefono)

    # Marca el mensaje como visto ANTES de procesarlo (dentro del lock) para
    # que si el webhook y el polling lo agarran casi al mismo tiempo, el
    # segundo lo vea ya procesado y no mande la respuesta dos veces. Por
    # eso todo lo que sigue va en un try/except amplio: si algo falla acá
    # abajo, el mensaje ya quedó marcado como visto y NO se va a reintentar
    # solo — hace falta que quede un log claro de qué pasó.
    with wecall_lock:
        contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
        with db.conectar() as con:
            fila = con.execute(
                "SELECT wecall_ultimo_id, modo_manual FROM contactos WHERE id = ?", (contacto_id,)
            ).fetchone()
        ya_visto = fila["wecall_ultimo_id"] if fila else None
        if ya_visto is not None and ya_visto >= mensaje_id:
            print(f"  ⏭️ (wecall) {telefono} descartado como ya visto (cursor={ya_visto}, mensaje_id={mensaje_id})")
            eventos.registrar("dedup_bloqueado", telefono, f"mensaje {mensaje_id} ya visto (cursor={ya_visto})")
            return
        db.actualizar_contacto(contacto_id, wecall_ultimo_id=mensaje_id)
        if not texto_mensaje:
            # Archivo sin texto (posible comprobante de pago): no lo lee el bot,
            # queda en la cola de la administración para revisarlo a mano.
            try:
                comp_id, pedido = seguridad.registrar_adjunto_whatsapp(contacto_id)
                eventos.registrar("adjunto_recibido", telefono, f"comprobante #{comp_id} pendiente de revisión")
                if pedido and not (fila and fila["modo_manual"]):
                    try:
                        wecall.enviar_mensaje(
                            telefono,
                            texto="Recibimos tu comprobante. Lo estamos validando y te avisamos en cuanto esté confirmado.",
                        )
                    except (VentanaCerradaError, EnvioFallidoError) as e:
                        eventos.registrar("envio_wecall", telefono, f"aviso de comprobante no enviado: {e}", ok=False)
            except Exception as e:
                import traceback
                traceback.print_exc()
                eventos.registrar("error", telefono, f"adjunto: {e}", ok=False)
            return
        if fila and fila["modo_manual"]:
            # Un humano está llevando esta conversación desde WeCall — el
            # bot solo avanza el cursor para no acumular backlog, pero no
            # responde nada solo.
            print(f"  🙋 (wecall) {telefono} está en modo manual, el bot no responde")
            eventos.registrar("modo_manual", telefono, "humano lleva la conversación")
            return

    try:
        try:
            contexto = wecall.obtener_contexto(telefono, limite=30)
            eventos.registrar("contexto_wecall", telefono, f"{len(contexto.get('mensajes', [])) if contexto else 0} mensajes")
        except Exception as e:
            print(f"  (wecall) no pude traer contexto de {telefono}: {e}")
            eventos.registrar("contexto_wecall", telefono, str(e), ok=False)
            contexto = None

        historial_previo = None
        if contexto:
            nombre = (contexto.get("contacto") or {}).get("nombre")
            if nombre:
                db.actualizar_contacto(contacto_id, nombre=nombre)
            historial_previo = _mapear_historial_wecall(contexto.get("mensajes", []), excluir_id=mensaje_id)

        print(f"📩 (wecall) {telefono}: {texto_mensaje}")
        agente = Agente(telefono=telefono)
        eventos.registrar("agente_iniciado", telefono, "vendedor " + agente.tipo, agente_tipo=agente.tipo)
        respuesta = agente.procesar_mensaje(texto_mensaje, historial_previo=historial_previo)
        print(f"🤖 (wecall) → {telefono}: {respuesta}")
        eventos.registrar("respuesta_generada", telefono, respuesta[:80], agente_tipo=agente.tipo)

        try:
            wecall.enviar_mensaje(telefono, texto=respuesta)
            eventos.registrar("envio_wecall", telefono, "enviado", agente_tipo=agente.tipo)
            # Avisos del sistema que van justo después de la respuesta (ej. total y datos de pago).
            for extra in getattr(agente, "mensajes_posteriores", []):
                ok_extra, motivo_extra = ciclo.notificar_cliente(agente.contacto_id, extra)
                if not ok_extra:
                    eventos.registrar("envio_wecall", telefono, f"aviso posterior no enviado: {motivo_extra}", ok=False)
        except VentanaCerradaError:
            print(f"  ⚠️ (wecall) ventana de 24h cerrada para {telefono} — hace falta responder con una plantilla aprobada")
            eventos.registrar("envio_wecall", telefono, "ventana de 24h cerrada", ok=False, agente_tipo=agente.tipo)
        except EnvioFallidoError as e:
            print(f"  ❌ (wecall) Meta rechazó el envío a {telefono}: {e.detalle}")
            eventos.registrar("envio_wecall", telefono, f"Meta rechazó: {e.detalle}", ok=False, agente_tipo=agente.tipo)
    except Exception as e:
        import traceback
        print(f"  ❌ (wecall) error procesando mensaje {mensaje_id} de {telefono} (ya quedó marcado como visto, no se reintenta solo):")
        traceback.print_exc()
        eventos.registrar("error", telefono, str(e), ok=False)


@app.route("/webhook/wecall", methods=["POST"])
def wecall_webhook():
    cuerpo_crudo = request.get_data()
    firma = request.headers.get("X-WeCall-Signature", "")

    # Log del body crudo ANTES de cualquier validación — así queda
    # evidencia de que el webhook llegó aunque la firma o el evento no
    # sean los esperados.
    print(f"🔔 (wecall) webhook recibido, {len(cuerpo_crudo)} bytes: {cuerpo_crudo[:500]!r}")

    if not wecall.verificar_firma(cuerpo_crudo, firma):
        print(f"  ⚠️ (wecall) firma inválida en el webhook, ignorado (header recibido: {firma!r})")
        eventos.registrar("firma_invalida", None, "firma inválida en /webhook/wecall", ok=False)
        return ("firma inválida", 401)

    payload = request.get_json(silent=True) or {}
    evento = payload.get("evento")
    if evento == "mensaje_nuevo":
        contacto = payload.get("contacto") or {}
        mensaje = payload.get("mensaje") or {}
        telefono = contacto.get("telefono", "")
        if telefono and mensaje:
            eventos.registrar("webhook_recibido", telefono, "mensaje_nuevo")
            threading.Thread(
                target=procesar_mensaje_wecall, args=(telefono, mensaje), daemon=True
            ).start()
        else:
            print(f"  ⚠️ (wecall) evento 'mensaje_nuevo' sin teléfono o sin mensaje: {payload}")
            eventos.registrar("webhook_recibido", None, "mensaje_nuevo sin teléfono o mensaje", ok=False)
    else:
        print(f"  (wecall) webhook con evento distinto de 'mensaje_nuevo': {evento!r} — ignorado")

    # Responder rápido — WeCall solo espera 6s y no reintenta.
    return ("", 200)


@app.route("/debug/wecall-test/<telefono>", methods=["GET"])
def wecall_debug_test(telefono):
    """DIAGNÓSTICO TEMPORAL — no manda nada, solo corre la misma cadena de
    procesamiento de forma síncrona y devuelve el error real si lo hay,
    en vez de que se pierda en un hilo en segundo plano."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    import traceback
    telefono = db.normalizar_telefono(telefono)
    salida = {"telefono": telefono}
    try:
        contexto = wecall.obtener_contexto(telefono, limite=30)
        salida["contexto_ok"] = contexto is not None
        salida["mensajes_en_contexto"] = len(contexto.get("mensajes", [])) if contexto else 0
        historial_previo = _mapear_historial_wecall(contexto.get("mensajes", [])) if contexto else None
        salida["historial_previo"] = historial_previo
        agente = Agente(telefono=telefono)
        respuesta = agente.procesar_mensaje("prueba de diagnóstico, ignora este mensaje y responde solo 'ok'", historial_previo=historial_previo)
        salida["respuesta_agente"] = respuesta
        salida["exito"] = True
    except Exception as e:
        salida["exito"] = False
        salida["error_tipo"] = type(e).__name__
        salida["error_msg"] = str(e)
        salida["traceback"] = traceback.format_exc()
    return jsonify(salida)


@app.route("/debug/set-tipo/<telefono>", methods=["POST"])
def debug_set_tipo(telefono):
    """DIAGNÓSTICO / ADMIN — cambia el tipo de un contacto (cliente/mayorista)
    para poder probar el vendedor mayorista con un número real."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    tipo = request.args.get("tipo")
    if tipo not in ("cliente", "mayorista"):
        return jsonify({"error": "usa ?tipo=cliente o ?tipo=mayorista"}), 400
    telefono = db.normalizar_telefono(telefono)
    contacto_id = db.obtener_o_crear_contacto(telefono, tipo=tipo)
    db.actualizar_contacto(contacto_id, tipo=tipo)
    return jsonify({"telefono": telefono, "contacto_id": contacto_id, "tipo_nuevo": tipo})


@app.route("/debug/wecall-reset-cursor/<telefono>", methods=["POST"])
def wecall_debug_reset_cursor(telefono):
    """DIAGNÓSTICO TEMPORAL — para deshacer el atasco que dejan mensajes
    sintéticos de prueba con id muy alto: baja wecall_ultimo_id al id
    real que se indique (por query ?hasta=N), para que el polling y el
    webhook puedan volver a procesar mensajes reales que quedaron
    bloqueados por el chequeo de duplicados."""
    if not _token_valido():
        return jsonify({"error": "no autorizado"}), 401
    hasta = request.args.get("hasta", type=int)
    if hasta is None:
        return jsonify({"error": "falta ?hasta=<id>"}), 400
    telefono = db.normalizar_telefono(telefono)
    contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
    db.actualizar_contacto(contacto_id, wecall_ultimo_id=hasta)
    return jsonify({"telefono": telefono, "contacto_id": contacto_id, "wecall_ultimo_id_nuevo": hasta})


@app.route("/api/wecall/enlace/<telefono>", methods=["GET"])
def wecall_enlace_conversacion(telefono):
    """Para el botón 'Ver WhatsApp' del CRM: arma el link al chat completo en WeCall."""
    try:
        datos = wecall.generar_enlace_conversacion(db.normalizar_telefono(telefono), minutos_validez=60)
        return jsonify(datos)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _loop_polling_wecall():
    """Respaldo del webhook: revisa cada tanto si algún contacto tiene mensajes
    nuevos que el webhook no haya avisado (es 'best-effort', sin reintentos)."""
    intervalo = int(os.environ.get("WECALL_POLL_INTERVAL_SEGUNDOS", 90))
    con_error = set()  # teléfonos que ya fallaron en el ciclo anterior, para no repetir el aviso
    while True:
        time.sleep(intervalo)
        try:
            for c in db.listar_contactos_wecall():
                try:
                    nuevos = wecall.obtener_mensajes_nuevos(c["telefono"], desde_id=c["wecall_ultimo_id"])
                    con_error.discard(c["telefono"])
                except Exception as e:
                    if c["telefono"] not in con_error:
                        print(f"  (wecall-poll) error consultando {c['telefono']}: {e}")
                        con_error.add(c["telefono"])
                    continue
                for m in sorted(nuevos, key=lambda x: x.get("id", 0)):
                    if m.get("direccion") == "entrante":
                        procesar_mensaje_wecall(c["telefono"], m)
                    else:
                        # Mensaje saliente que no pasó por nuestro propio envío
                        # (ej. alguien contestó a mano desde WeCall): solo
                        # avanzamos el cursor para no volver a revisarlo.
                        mid = m.get("id")
                        if mid is not None:
                            with wecall_lock:
                                db.actualizar_contacto(c["id"], wecall_ultimo_id=mid)
        except Exception as e:
            print(f"  (wecall-poll) error en el ciclo: {e}")


if os.environ.get("WECALL_API_KEY") and os.environ.get("WECALL_WEBHOOK_SECRET"):
    threading.Thread(target=_loop_polling_wecall, daemon=True).start()


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
