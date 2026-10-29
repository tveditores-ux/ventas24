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
import uuid
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify, redirect, send_from_directory, session
from twilio.rest import Client
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

import db
import analista
import ciclo
import bcv
import contable
import erp
import voz
import importacion
import zipfile
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
    return jsonify({"id": usuario["id"], "nombre": usuario["nombre"], "rol": usuario["rol"],
                    "debe_cambiar": bool(usuario.get("debe_cambiar"))})


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


@app.route("/api/crm/cambiar-password", methods=["POST"])
@requiere_sesion()
def api_cambiar_password():
    d = request.get_json(silent=True) or {}
    u = db.obtener_usuario(request.usuario["id"])
    if not check_password_hash(u["password_hash"], d.get("actual") or ""):
        return jsonify({"error": "la contraseña actual no es correcta"}), 400
    nueva = d.get("nueva") or ""
    if len(nueva) < 10:
        return jsonify({"error": "la contraseña nueva debe tener al menos 10 caracteres"}), 400
    if nueva == (d.get("actual") or ""):
        return jsonify({"error": "la contraseña nueva debe ser distinta de la actual"}), 400
    db.actualizar_usuario(u["id"], password_hash=generate_password_hash(nueva), debe_cambiar=0)
    db.registrar_accion("Usuarios", None, "cambiar_password", {"usuario": u["email"]}, "ok")
    return jsonify({"ok": True})


def _clave_temporal() -> str:
    import secrets
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sin letras que se confunden (l, I, O, 0, 1)
    return "".join(secrets.choice(alfabeto) for _ in range(12))


@app.route("/api/admin/respaldo", methods=["GET"])
@requiere_sesion(roles=["super_admin"])
def api_respaldo():
    """Descarga una copia completa de la base de datos (SQLite, comprimida). Solo super administrador."""
    import gzip, sqlite3, tempfile
    fd, ruta = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        origen = sqlite3.connect(db.RUTA_DB)
        destino = sqlite3.connect(ruta)
        with destino:
            origen.backup(destino)       # copia consistente aunque el sistema esté escribiendo
        origen.close(); destino.close()
        with open(ruta, "rb") as f:
            datos = gzip.compress(f.read(), compresslevel=6)
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass
    db.registrar_accion("Respaldo", None, "descargar_respaldo", {"bytes": len(datos)}, request.usuario["nombre"])
    nombre = "ventas24-respaldo-" + time.strftime("%Y%m%d-%H%M") + ".db.gz"
    return Response(datos, mimetype="application/gzip", headers={"Content-Disposition": f'attachment; filename="{nombre}"', "Cache-Control": "no-store"})


@app.route("/api/crm/usuarios", methods=["GET", "POST"])
@requiere_sesion(roles=["super_admin"])
def api_usuarios():
    if request.method == "GET":
        return jsonify(db.listar_usuarios())
    d = request.get_json(silent=True) or {}
    nombre, email, rol = (d.get("nombre") or "").strip(), (d.get("email") or "").strip().lower(), d.get("rol") or "vendedor"
    if not nombre or "@" not in email:
        return jsonify({"error": "falta el nombre o el correo es inválido"}), 400
    if rol not in db.ROLES_VALIDOS:
        return jsonify({"error": "rol inválido"}), 400
    temporal = _clave_temporal()
    try:
        uid = db.crear_usuario(nombre, email, generate_password_hash(temporal), rol)
    except Exception:
        return jsonify({"error": "ya existe un usuario con ese correo"}), 400
    db.actualizar_usuario(uid, debe_cambiar=1)
    db.registrar_accion("Usuarios", None, "crear_usuario", {"email": email, "rol": rol}, request.usuario["nombre"])
    return jsonify({"id": uid, "nombre": nombre, "email": email, "rol": rol, "clave_temporal": temporal})


@app.route("/api/crm/usuarios/<int:uid>", methods=["POST"])
@requiere_sesion(roles=["super_admin"])
def api_usuario_editar(uid):
    d = request.get_json(silent=True) or {}
    u = db.obtener_usuario(uid)
    if not u:
        return jsonify({"error": "usuario no encontrado"}), 404
    propio = uid == request.usuario["id"]
    cambios, salida = {}, {}
    if "rol" in d:
        if d["rol"] not in db.ROLES_VALIDOS:
            return jsonify({"error": "rol inválido"}), 400
        if propio and d["rol"] != u["rol"]:
            return jsonify({"error": "no puedes cambiar tu propio rol"}), 400
        cambios["rol"] = d["rol"]
    if "activo" in d:
        if propio and not d["activo"]:
            return jsonify({"error": "no puedes desactivar tu propia cuenta"}), 400
        cambios["activo"] = 1 if d["activo"] else 0
    if d.get("reiniciar_clave"):
        temporal = _clave_temporal()
        cambios["password_hash"] = generate_password_hash(temporal); cambios["debe_cambiar"] = 1
        salida["clave_temporal"] = temporal
    if not cambios:
        return jsonify({"error": "nada que cambiar"}), 400
    db.actualizar_usuario(uid, **cambios)
    db.registrar_accion("Usuarios", None, "editar_usuario", {"email": u["email"], "cambios": [k for k in cambios if k != "password_hash"]}, request.usuario["nombre"])
    return jsonify({"ok": True, **salida})


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


# ---------------------------------------------------------------------
# ERP: inventario, proveedores y compras (solo administración)
# ---------------------------------------------------------------------

def _erp(funcion, *args, **kwargs):
    """Ejecuta una operación del ERP y convierte sus errores de validación en respuestas 400."""
    try:
        return jsonify(funcion(*args, **kwargs))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/erp/resumen", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_resumen():
    return jsonify(erp.resumen_inventario())


@app.route("/api/erp/inventario", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_inventario():
    productos, total = erp.inventario(request.args.get("q"), request.args.get("bajo") == "1")
    return jsonify({"productos": productos, "total": total})


@app.route("/api/erp/inventario/<int:catalogo_id>/conteo", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_conteo(catalogo_id):
    d = request.get_json(silent=True) or {}
    try:
        cantidad = int(d.get("cantidad"))
        minimo = int(d["minimo"]) if str(d.get("minimo", "")).strip() != "" else None
        costo = float(str(d["costo"]).replace(",", ".")) if str(d.get("costo", "")).strip() != "" else None
    except (TypeError, ValueError):
        return jsonify({"error": "cantidad, mínimo y costo deben ser números"}), 400
    return _erp(erp.contar, catalogo_id, cantidad, request.usuario["id"], (d.get("nota") or "").strip() or None, minimo, costo)


@app.route("/api/erp/inventario/carga", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_carga():
    """CSV en el cuerpo con columnas codigo,cantidad y opcionales marca,costo,minimo."""
    import csv
    import io
    texto = request.get_data().decode("utf-8-sig", errors="replace")
    muestra = texto[:2000]
    delimitador = ";" if muestra.count(";") > muestra.count(",") else ","
    filas = list(csv.DictReader(io.StringIO(texto), delimiter=delimitador))
    if not filas or "codigo" not in (filas[0].keys() if filas else []) or "cantidad" not in filas[0].keys():
        return jsonify({"error": "el CSV necesita las columnas codigo y cantidad (opcionales: marca, costo, minimo)"}), 400
    resultado = erp.cargar_conteo([{k.strip().lower(): v for k, v in f.items() if k} for f in filas], request.usuario["id"])
    return jsonify(resultado)


@app.route("/api/erp/movimientos", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_movimientos():
    return jsonify(erp.movimientos(request.args.get("producto", type=int)))


@app.route("/api/erp/reportes", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_reportes():
    return jsonify(erp.reportes(request.args.get("dias", default=30, type=int)))


@app.route("/api/erp/compras/borradores", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_borradores():
    """El agente de compras arma borradores con lo que está bajo el mínimo. Aprobarlos sigue siendo humano."""
    hecho = erp.borradores_reposicion(request.usuario["id"])
    db.registrar_accion("Compras", None, "borradores_reposicion", {"usuario": request.usuario["nombre"]}, hecho)
    return jsonify(hecho)


@app.route("/api/erp/auditoria", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_auditoria():
    return jsonify(db.listar_acciones(request.args.get("limite", default=100, type=int)))


@app.route("/api/erp/analista", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_analista():
    pregunta = ((request.get_json(silent=True) or {}).get("pregunta") or "").strip()
    if len(pregunta) < 3:
        return jsonify({"error": "escribe una pregunta"}), 400
    try:
        respuesta = analista.preguntar(pregunta[:800], request.usuario["nombre"])
    except Exception as e:
        eventos.registrar("error", "-", f"analista: {e}", ok=False)
        return jsonify({"error": "El analista no pudo responder ahora. Intenta de nuevo en un momento."}), 502
    return jsonify({"respuesta": respuesta})


@app.route("/api/crm/pedidos/<int:pedido_id>/credito", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def crm_autorizar_credito(pedido_id):
    """Excepción aprobada por un administrador: el pedido puede despacharse sin pago validado, hasta el límite
    de crédito del cliente. Queda anotado quién lo autorizó, hasta cuándo y por qué."""
    d = request.get_json(silent=True) or {}
    try:
        resumen = db.autorizar_credito(pedido_id, request.usuario["id"], int(d.get("dias") or 0), (d.get("nota") or "").strip() or None)
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e) or "datos inválidos"}), 409
    pedido = db.obtener_pedido(pedido_id)
    eventos.registrar("credito", pedido["telefono"], f"pedido {pedido_id} a crédito hasta {resumen['vence']} por {request.usuario['nombre']}")
    db.registrar_accion("Administración", pedido["contacto_id"], "autorizar_credito", {"pedido": pedido_id, **d}, resumen)
    return jsonify(resumen)


@app.route("/api/erp/clientes/limite", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_limite_credito():
    d = request.get_json(silent=True) or {}
    telefono = db.normalizar_telefono((d.get("telefono") or "").strip())
    try:
        monto = float(str(d.get("monto")).replace(",", "."))
    except (TypeError, ValueError):
        return jsonify({"error": "el monto debe ser un número"}), 400
    with db.conectar() as con:
        fila = con.execute("SELECT id, nombre FROM contactos WHERE telefono = ?", (telefono,)).fetchone()
    if not fila:
        return jsonify({"error": "no hay ningún contacto con ese teléfono (usa el formato +58…)"}), 404
    try:
        db.fijar_limite_credito(fila["id"], monto)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.registrar_accion("Administración", fila["id"], "fijar_limite_credito", {"monto": monto}, request.usuario["nombre"])
    return jsonify({"cliente": fila["nombre"] or telefono, "limite": monto})


@app.route("/api/erp/reglas", methods=["GET", "POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_reglas():
    """Reglas comerciales: tasa del día, vigencia, días de reserva y pedido mínimo al mayor."""
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        def num(k, minimo=0):
            try:
                v = float(str(d.get(k)).replace(",", "."))
            except (TypeError, ValueError):
                raise ValueError(f"{k} debe ser un número")
            if v <= minimo and not (minimo == 0 and v == 0):
                raise ValueError(f"{k} debe ser mayor que {minimo}")
            return v
        try:
            hecho = {}
            if d.get("tasa") not in (None, ""):
                db.guardar_tasa(num("tasa"), (d.get("fuente") or "manual").strip()[:40], eur=num("eur") if d.get("eur") not in (None, "") else None)
                db.guardar_config("tasa_auto", "0"); hecho["tasa"] = d["tasa"]   # a mano: el automático no la pisa hasta que lo reactives
            for k, clave in (("reserva_dias", "reserva_dias"), ("pedido_minimo_usd", "pedido_minimo_usd"),
                             ("pedido_minimo_unidades", "pedido_minimo_unidades"), ("tasa_vigencia_dias", "tasa_vigencia_dias"),
                             ("margen_max_pct", "margen_max_pct"), ("nota_entrega_alerta_pct", "nota_entrega_alerta_pct")):
                if d.get(k) not in (None, ""):
                    db.guardar_config(clave, str(num(k))); hecho[k] = d[k]
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        db.registrar_accion("Administración", None, "cambiar_reglas", hecho, request.usuario["nombre"])
    return jsonify({"tasa": db.tasa_vigente(), "reserva_dias": db.reserva_dias(), "pedido_minimo": db.pedido_minimo(),
                    "tasa_vigencia_dias": db._num_config("tasa_vigencia_dias", 1), "margen_max_pct": db._num_config("margen_max_pct", 30), "nota_entrega_alerta_pct": db._num_config("nota_entrega_alerta_pct", 10)})


@app.route("/api/erp/importacion", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_importacion_lista():
    return jsonify({"embarques": importacion.listar()})


@app.route("/api/erp/importacion/analizar", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_importacion_analizar():
    archivos = {}
    for f in request.files.getlist("archivos"):
        datos = f.read(6_000_001)
        if len(datos) > 6_000_000:
            return jsonify({"error": f"{f.filename} pesa demasiado (máx. 6 MB)"}), 400
        archivos[f.filename or "archivo"] = datos
    if not archivos:
        return jsonify({"error": "sube el ZIP o los 4 CSV de la liquidación"}), 400
    try:
        return jsonify(importacion.analizar(archivos, request.usuario["id"]))
    except (ValueError, zipfile.BadZipFile) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/erp/importacion/<int:eid>", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_importacion_ver(eid):
    e = importacion.obtener(eid)
    return (jsonify(e), 200) if e else (jsonify({"error": "no encontrado"}), 404)


@app.route("/api/erp/importacion/<int:eid>/tasa-aduana", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_importacion_tasa_aduana(eid):
    d = request.get_json(silent=True) or {}
    try:
        importacion.fijar_tasa_aduana(eid, float(str(d.get("tasa")).replace(",", ".")))
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e) if isinstance(e, ValueError) else "la tasa debe ser un número"}), 400
    return jsonify(importacion.obtener(eid))


@app.route("/api/erp/importacion/<int:eid>/<accion>", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_importacion_accion(eid, accion):
    d = request.get_json(silent=True) or {}
    try:
        if accion == "aprobar":
            return jsonify(importacion.aprobar(eid, request.usuario["id"], bool(d.get("acepto_revisar"))))
        if accion == "recibir":
            return jsonify(importacion.recibir(eid, request.usuario["id"], d.get("recibidos")))
        if accion == "descartar":
            importacion.descartar(eid)
            return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": "acción desconocida"}), 404


@app.route("/api/erp/tasa", methods=["GET"])
@requiere_sesion()
def erp_tasa():
    """Tasa BCV para mostrarla en la cabecera del CRM (cualquier usuario con sesión)."""
    return jsonify(db.tasa_vigente())


@app.route("/api/erp/tasa/actualizar", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_tasa_actualizar():
    r = bcv.actualizar()
    if r["ok"]:
        db.registrar_accion("Administración", None, "actualizar_tasa_bcv", r, request.usuario["nombre"])
    return jsonify({**r, "tasa": db.tasa_vigente()}), (200 if r["ok"] else 502)


# ---------------------------------------------------------------------
# Canal de voz (Twilio). Ver voz.py.
# ---------------------------------------------------------------------

def _url_voz(ruta: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or request.url_root.rstrip("/")
    return base + ruta


def _twiml(xml: str):
    return Response(xml, mimetype="text/xml")


def _voz_protegida(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        if not voz.firma_valida(request):
            return Response("firma de Twilio inválida o canal de voz sin configurar", status=403)
        return f(*args, **kwargs)
    return envoltura


@app.route("/voz/entrante", methods=["POST"])
@_voz_protegida
def voz_entrante():
    telefono = request.form.get("To") if request.args.get("saliente") else request.form.get("From")
    callsid = request.form.get("CallSid", "")
    saludo = voz.iniciar(telefono or "desconocido", callsid, saliente=bool(request.args.get("saliente")))
    return _twiml(voz.twiml_hablar(voz.voz_vigilante.para_hablar(saludo), _url_voz("/voz/turno")))


@app.route("/voz/turno", methods=["POST"])
@_voz_protegida
def voz_turno():
    callsid = request.form.get("CallSid", "")
    telefono = (request.form.get("To") if request.form.get("Direction", "").startswith("outbound") else request.form.get("From")) or "desconocido"
    texto = (request.form.get("SpeechResult") or "").strip()
    try:
        confianza = float(request.form.get("Confidence") or 1)
    except ValueError:
        confianza = 1.0
    url = _url_voz("/voz/turno")
    try:
        if not texto:
            if voz.contar(callsid, "silencios") > voz.MAX_SILENCIOS:
                return _twiml(voz.twiml_hablar("Parece que no te escucho. Te escribo por WhatsApp. Un saludo.", url, colgar=True))
            return _twiml(voz.twiml_hablar("¿Sigues ahí? Cuéntame qué necesitas.", url))
        if confianza < voz.CONFIANZA_MINIMA and voz.contar(callsid, "incomprendidos") <= voz.MAX_INCOMPRENDIDOS:
            return _twiml(voz.twiml_hablar("Disculpa, no te escuché bien. ¿Me lo repites?", url))
        voz.reiniciar_contadores(callsid)
        r = voz.responder(telefono, callsid, texto)
        return _twiml(voz.twiml_hablar(r["texto"], url, colgar=r["colgar"]))
    except Exception as e:
        import traceback
        traceback.print_exc()
        eventos.registrar("error", telefono, f"voz: {e}", ok=False)
        try:
            cid = db.obtener_o_crear_contacto(telefono, tipo="mayorista")
            db.crear_solicitud(cid, "otro", "Llamada con falla técnica: escribirle por WhatsApp", f"El cliente dijo: {texto[:300]}")
        except Exception:
            pass
        return _twiml(voz.twiml_hablar("Tuve un problema técnico. Una persona del equipo te escribe por WhatsApp. Disculpa y gracias por llamar.", url, colgar=True))


@app.route("/voz/estado", methods=["POST"])
@_voz_protegida
def voz_estado():
    if request.form.get("CallStatus") in ("completed", "busy", "no-answer", "failed", "canceled"):
        try:
            seg = int(request.form.get("CallDuration") or 0)
        except ValueError:
            seg = 0
        voz.terminar(request.form.get("CallSid", ""), seg)
    return ("", 204)


@app.route("/voz-prueba", methods=["GET"])
def voz_prueba_page():
    """Pantalla para probar el bot de voz con el micrófono y los altavoces del navegador (sin Twilio)."""
    usuario = _usuario_actual()
    if not usuario:
        return redirect("/login?next=/voz-prueba")
    if usuario["rol"] not in ROLES_ADMIN:
        return "No autorizado", 403
    resp = send_from_directory(os.path.dirname(os.path.abspath(__file__)), "voz_prueba.html")
    resp.headers["Cache-Control"] = "no-store"; resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/api/voz/simular", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def api_voz_simular():
    """Prueba el cerebro de voz sin Twilio: manda texto como si fuera lo que el cliente dijo. No envía WhatsApp."""
    d = request.get_json(silent=True) or {}
    telefono = db.normalizar_telefono((d.get("telefono") or "").strip())
    if not telefono:
        return jsonify({"error": "falta el teléfono de prueba (formato +58…)"}), 400
    callsid = d.get("callsid") or "SIM" + uuid.uuid4().hex[:12]
    texto = (d.get("texto") or "").strip()
    if not texto:
        saludo = voz.iniciar(telefono, callsid)
        return jsonify({"callsid": callsid, "dice": voz.voz_vigilante.para_hablar(saludo), "colgar": False})
    r = voz.responder(telefono, callsid, texto, simulacion=True)
    return jsonify({"callsid": callsid, "dice": r["texto"], "crudo": r["crudo"], "colgar": r["colgar"], "whatsapp_que_saldria": r["whatsapp"],
                    "twiml": voz.twiml_hablar(r["texto"], _url_voz("/voz/turno"), colgar=r["colgar"])})


@app.route("/api/voz/llamar", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def api_voz_llamar():
    """Llamada saliente: solo a quien la espera (prueba o seguimiento acordado)."""
    d = request.get_json(silent=True) or {}
    try:
        sid = voz.llamar((d.get("telefono") or "").strip())
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    db.registrar_accion("Administración", None, "llamada_saliente", {"telefono": d.get("telefono")}, request.usuario["nombre"])
    return jsonify({"callsid": sid})


@app.route("/api/voz/llamadas", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def api_voz_llamadas():
    voz.asegurar_tablas()
    with db.conectar() as con:
        filas = con.execute(
            "SELECT v.*, c.nombre, c.telefono FROM voz_llamadas v LEFT JOIN contactos c ON c.id = v.contacto_id ORDER BY v.inicio DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(f) for f in filas])


@app.route("/api/erp/contabilidad", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_contabilidad():
    """Relación de ingresos (por moneda, método y tasa del día) y notas por diferencial cambiario, para la contadora."""
    return jsonify(db.relacion_ingresos(request.args.get("desde") or None, request.args.get("hasta") or None))


@app.route("/api/erp/contabilidad.csv", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_contabilidad_csv():
    import csv, io
    r = db.relacion_ingresos(request.args.get("desde") or None, request.args.get("hasta") or None)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["fecha", "cliente", "orden", "metodo", "moneda", "monto_original", "tasa_pago", "monto_usd", "monto_bs", "banco", "referencia"])
    for p in r["pagos"]:
        w.writerow([p["fecha"], p["nombre"] or p["telefono"], p["orden"], p["metodo"], p["moneda"], p["monto_original"], p["tasa_pago"], p["monto_usd"], p["monto_bs"], p["banco"], p["referencia"]])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="relacion-ingresos.csv"'})


@app.route("/api/erp/notas-cambiarias/<int:nid>", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_nota_cambiaria(nid):
    d = request.get_json(silent=True) or {}
    try:
        db.estado_nota_cambiaria(nid, d.get("estado"), (d.get("nota") or "").strip() or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.registrar_accion("Administración", None, "nota_cambiaria", {"id": nid, "estado": d.get("estado")}, request.usuario["nombre"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Reportes y soportes para la contadora (ver contable.py)
# ---------------------------------------------------------------------

def _rango_args():
    return request.args.get("desde") or None, request.args.get("hasta") or None


@app.route("/api/erp/reportes/<nombre>", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_reporte(nombre):
    if nombre not in contable.COLS:
        return jsonify({"error": "reporte desconocido"}), 404
    r = contable.reporte(nombre, *_rango_args())
    if request.args.get("formato") == "csv":
        return Response(contable.a_csv(contable.COLS[nombre], r["filas"]), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{nombre}.csv"'})
    return jsonify(r)


def _intentar(fn):
    try:
        return jsonify(fn())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/erp/ventas-doc/<int:pedido_id>", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_venta_doc(pedido_id):
    d = request.get_json(silent=True) or {}
    def hacer():
        did = contable.registrar_doc_venta(pedido_id, d.get("tipo"), d.get("numero"), d.get("control"), d.get("fecha"), d.get("motivo"), request.usuario["id"])
        db.registrar_accion("Administración", None, "documento_venta", {"pedido": pedido_id, "tipo": d.get("tipo"), "motivo": d.get("motivo")}, request.usuario["nombre"])
        return {"id": did}
    return _intentar(hacer)


@app.route("/api/erp/ventas-doc/<int:doc_id>/convertir", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_venta_doc_convertir(doc_id):
    d = request.get_json(silent=True) or {}
    def hacer():
        contable.convertir_en_factura(doc_id, d.get("numero"), d.get("control"), d.get("fecha"), request.usuario["id"])
        db.registrar_accion("Administración", None, "despacho_a_factura", {"doc": doc_id}, request.usuario["nombre"])
        return {"ok": True}
    return _intentar(hacer)


@app.route("/api/erp/compras", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_compra_nueva():
    d = request.get_json(silent=True) or {}
    return _intentar(lambda: {"id": contable.registrar_compra(d, request.usuario["id"])})


@app.route("/api/erp/bancos", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_banco_nuevo():
    d = request.get_json(silent=True) or {}
    return _intentar(lambda: {"id": contable.registrar_banco(d, request.usuario["id"])})


@app.route("/api/erp/adjuntos", methods=["GET", "POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_adjuntos():
    if request.method == "GET":
        try:
            return jsonify(contable.lista_adjuntos(request.args.get("entidad", ""), int(request.args.get("entidad_id", 0))))
        except ValueError:
            return jsonify({"error": "datos inválidos"}), 400
    f = request.files.get("archivo")
    if not f:
        return jsonify({"error": "falta el archivo"}), 400
    try:
        eid = int(request.form.get("entidad_id", 0))
    except ValueError:
        return jsonify({"error": "datos inválidos"}), 400
    return _intentar(lambda: contable.guardar_adjunto(request.form.get("entidad", ""), eid, f.filename or "", f.read(contable.MAX_ADJUNTO + 1), request.usuario["id"]))


@app.route("/api/erp/adjuntos/<int:adj_id>", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_adjunto_ver(adj_id):
    r = contable.leer_adjunto(adj_id)
    if not r:
        return jsonify({"error": "no encontrado"}), 404
    meta, ruta = r
    return send_from_directory(os.path.dirname(ruta), os.path.basename(ruta), mimetype=meta["mime"], as_attachment=False, download_name=meta["nombre"])


@app.route("/api/erp/carpeta-mensual", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_carpeta_mensual():
    try:
        datos = contable.carpeta_mensual(request.args.get("mes", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.registrar_accion("Administración", None, "carpeta_mensual", {"mes": request.args.get("mes")}, request.usuario["nombre"])
    return Response(datos, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="carpeta-{request.args.get("mes")}.zip"'})


@app.route("/api/erp/cobranza", methods=["GET"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_cobranza():
    cuentas = db.cuentas_por_cobrar()
    return jsonify({"cuentas": cuentas, "total": round(sum(c["total"] for c in cuentas), 2),
                    "vencido": round(sum(c["total"] for c in cuentas if c["atraso_dias"] > 0), 2)})


@app.route("/api/erp/cobranza/<int:pedido_id>/recordatorio", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_recordatorio(pedido_id):
    res = ciclo.aviso_cobranza(pedido_id)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 409
    db.registrar_accion("Cobranza", None, "recordatorio", {"pedido": pedido_id, "por": request.usuario["nombre"]}, res)
    return jsonify(res)


@app.route("/api/erp/documentos", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_crear_documento():
    d = request.get_json(silent=True) or {}
    try:
        doc_id = db.crear_documento_control(int(d.get("pedido_id")), request.usuario["id"], bool(d.get("en_divisas", True)))
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e) or "pedido inválido"}), 409
    return jsonify({"id": doc_id, "url": f"/doc/{doc_id}"})


@app.route("/doc/<int:documento_id>", methods=["GET"])
def documento_control(documento_id):
    """Página imprimible del documento de control interno. Exige sesión de administración."""
    from html import escape as e
    usuario = _usuario_actual()
    if not usuario:
        return redirect("/login?next=" + request.path)
    if usuario["rol"] not in ROLES_ADMIN:
        return "No autorizado", 403
    doc = db.obtener_documento(documento_id)
    if not doc:
        return "Documento no encontrado", 404
    filas = "".join(
        f"<tr><td>{e(str(l['cantidad']))}</td><td>{e(l['producto'])}</td><td class='n'>${l['precio_unitario']:.2f}</td><td class='n'>${l['total']:.2f}</td></tr>"
        for l in doc["lineas"]
    )
    fecha = (doc["creado_en"] or "")[:10]
    return Response(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Documento de control {doc['numero']:06d}</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;color:#152033;max-width:760px;margin:24px auto;padding:0 16px;position:relative}}
 .marca{{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:0}}
 .marca span{{font-size:64px;font-weight:800;color:rgba(190,58,43,.10);transform:rotate(-24deg);text-align:center;line-height:1.1}}
 .aviso{{background:#FBE4E0;color:#7a2018;border:1px solid #e9b7ae;border-radius:10px;padding:10px 14px;font-size:13px;font-weight:600;margin-bottom:18px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#55627A;font-size:13px;margin-bottom:18px}}
 table{{width:100%;border-collapse:collapse;position:relative;z-index:1;font-size:14px}} th,td{{padding:9px 8px;border-bottom:1px solid #E1E7F0;text-align:left}}
 th{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#55627A}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
 .tot td{{border:none;padding:4px 8px}} .tot .g td{{font-weight:800;font-size:16px;border-top:2px solid #152033;padding-top:10px}}
 button{{margin-top:22px;padding:10px 18px;border-radius:9px;border:0;background:#2B5C8F;color:#fff;font-weight:700;cursor:pointer}} @media print{{button{{display:none}}}}
</style></head><body>
<div class="marca"><span>CONTROL INTERNO<br>NO FISCAL</span></div>
<div class="aviso">DOCUMENTO DE PRUEBA DE CONTROL INTERNO. No es una factura fiscal ni tiene validez ante el SENIAT. Las tasas deben validarse con el contador.</div>
<h1>Documento de control N.º {doc['numero']:06d}</h1>
<div class="sub">Fecha {e(fecha)} · Cliente: {e(doc['nombre'] or doc['telefono'])} · Tel. {e(doc['telefono'])}</div>
<table><thead><tr><th>Cant.</th><th>Producto</th><th class="n">Precio</th><th class="n">Total</th></tr></thead><tbody>{filas}</tbody></table>
<table class="tot"><tr><td></td><td class="n">Base imponible</td><td class="n">${doc['base']:.2f}</td></tr>
<tr><td></td><td class="n">IVA {doc['iva_pct']:g} %</td><td class="n">${doc['iva']:.2f}</td></tr>
<tr><td></td><td class="n">IGTF {doc['igtf_pct']:g} %</td><td class="n">${doc['igtf']:.2f}</td></tr>
<tr class="g"><td></td><td class="n">Total</td><td class="n">${doc['total']:.2f}</td></tr></table>
<button onclick="window.print()">Imprimir</button></body></html>""", mimetype="text/html")


@app.route("/api/erp/proveedores", methods=["GET", "POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_proveedores():
    if request.method == "GET":
        return jsonify(erp.listar_proveedores())
    d = request.get_json(silent=True) or {}
    try:
        nuevo = erp.crear_proveedor(d.get("nombre"), d.get("contacto"), d.get("telefono"), d.get("notas"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": nuevo})


@app.route("/api/erp/compras", methods=["GET", "POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_compras():
    if request.method == "GET":
        return jsonify({"ordenes": erp.listar_ordenes(request.args.get("estado")), "sugeridos": erp.sugerir_reposicion()})
    d = request.get_json(silent=True) or {}
    try:
        orden = erp.crear_orden_compra(int(d.get("proveedor_id")), d.get("lineas") or [], (d.get("nota") or "").strip() or None,
                                       request.usuario["id"])
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e) if isinstance(e, ValueError) and str(e) else "proveedor inválido"}), 400
    return jsonify({"id": orden})


@app.route("/api/erp/compras/<int:orden_id>/<accion>", methods=["POST"])
@requiere_sesion(roles=ROLES_ADMIN)
def erp_compra_accion(orden_id, accion):
    try:
        if accion == "aprobar":
            erp.aprobar_orden(orden_id, request.usuario["id"])
            return jsonify({"ok": True})
        if accion == "cancelar":
            erp.cancelar_orden(orden_id)
            return jsonify({"ok": True})
        if accion == "recibir":
            recibidos = (request.get_json(silent=True) or {}).get("recibidos")
            repuestos = erp.recibir_orden(orden_id, request.usuario["id"], recibidos)
            avisos = ciclo.procesar_eventos() if repuestos else {"avisados": 0, "sin_aviso": 0, "eventos": 0}
            eventos.registrar("compra_recibida", "-", f"orden #{orden_id} recibida por {request.usuario['nombre']}")
            return jsonify({"ok": True, "repuestos": repuestos, "lista_de_espera": avisos})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"error": "acción desconocida"}), 404


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
    conteos = db.contar_pendientes(usuario)
    if usuario["rol"] in ROLES_ADMIN:
        r = erp.resumen_inventario()
        conteos["stock_bajo"] = r["bajos"]
        conteos["compras_borrador"] = r["compras_borrador"]
    return jsonify({
        "conteos": conteos,
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
            ciclo.procesar_eventos()   # reintenta avisos y compras automáticas pendientes
            ciclo.liberar_reservas_vencidas()
        except Exception as e:
            print(f"  (wecall-poll) error en el ciclo: {e}")


if os.environ.get("TASA_BCV_AUTO", "1") != "0" and not os.environ.get("PYTEST_CURRENT_TEST"):
    bcv.iniciar()

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
