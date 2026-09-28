"""
Base de datos propia del canal de Mercado Libre (meli.db).

Está separada a propósito de crm.db: este módulo no lee ni escribe nada
del sistema de WhatsApp, así que no puede romper lo que ya funciona.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "MELI_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "meli.db"),
)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS credenciales (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    user_id INTEGER,
    access_token TEXT,
    refresh_token TEXT,
    expira_en TEXT
);

-- Todo lo que el sistema quiere publicar en Mercado Libre pasa por acá:
-- respuestas a preguntas y mensajes de postventa. Según el modo
-- (auto / aprobar) se envía enseguida o espera tu aprobación en el panel.
CREATE TABLE IF NOT EXISTS salientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo TEXT NOT NULL,              -- 'respuesta' | 'mensaje'
    referencia TEXT NOT NULL,        -- question_id o pack_id
    orden_id TEXT,
    texto TEXT NOT NULL,
    estado TEXT NOT NULL,            -- pendiente | enviado | descartado | error
    motivo TEXT,                     -- por qué quedó pendiente (si aplica)
    error TEXT,
    creado TEXT NOT NULL,
    enviado TEXT
);

CREATE TABLE IF NOT EXISTS preguntas (
    id TEXT PRIMARY KEY,             -- question_id de Mercado Libre
    item_id TEXT,
    item_titulo TEXT,
    comprador_id TEXT,
    texto TEXT,
    creado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ordenes (
    id TEXT PRIMARY KEY,             -- order_id
    pack_id TEXT NOT NULL,
    comprador_id TEXT,
    comprador_apodo TEXT,
    comprador_nombre TEXT,
    detalle TEXT,                    -- productos (texto legible)
    total REAL,
    moneda TEXT,
    estado TEXT NOT NULL,            -- ver ESTADOS_ORDEN
    bot_activo INTEGER NOT NULL DEFAULT 1,
    datos_envio TEXT,                -- JSON
    guia TEXT,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mensajes (
    meli_id TEXT PRIMARY KEY,        -- id del mensaje en Mercado Libre (o local-N)
    pack_id TEXT NOT NULL,
    rol TEXT NOT NULL,               -- 'comprador' | 'vendedor'
    texto TEXT NOT NULL,
    fecha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pagos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id TEXT NOT NULL,
    metodo TEXT,
    banco TEXT,
    referencia TEXT,
    monto TEXT,
    fecha_pago TEXT,
    titular TEXT,
    estado TEXT NOT NULL,            -- por_verificar | confirmado | rechazado
    nota TEXT,
    creado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notificaciones_vistas (
    clave TEXT PRIMARY KEY,
    creado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS avisos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    texto TEXT NOT NULL,
    canal TEXT,                      -- por dónde salió (o 'solo_panel')
    creado TEXT NOT NULL
);
"""

ESTADOS_ORDEN = {
    "nueva": "Nueva",
    "esperando_pago": "Esperando pago",
    "pago_reportado": "Pago por verificar",
    "pago_confirmado": "Pago confirmado — preparar envío",
    "enviada": "Enviada",
    "cancelada": "Cancelada",
}


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conectar():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def inicializar():
    with conectar() as con:
        con.executescript(ESQUEMA)


inicializar()


def _dicts(filas):
    return [dict(f) for f in filas]


# --- credenciales ---------------------------------------------------------

def leer_credenciales():
    with conectar() as con:
        fila = con.execute("SELECT * FROM credenciales WHERE id = 1").fetchone()
    return dict(fila) if fila else None


def guardar_credenciales(user_id, access_token, refresh_token, expira_en):
    with conectar() as con:
        con.execute(
            """INSERT INTO credenciales (id, user_id, access_token, refresh_token, expira_en)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,
                 access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                 expira_en=excluded.expira_en""",
            (user_id, access_token, refresh_token, expira_en),
        )


# --- deduplicación de notificaciones -------------------------------------

def marcar_si_nueva(clave):
    """Devuelve True la primera vez que se ve esta clave, False si ya se procesó."""
    with conectar() as con:
        try:
            con.execute("INSERT INTO notificaciones_vistas (clave, creado) VALUES (?, ?)", (clave, ahora()))
            return True
        except sqlite3.IntegrityError:
            return False


# --- salientes --------------------------------------------------------------

def crear_saliente(tipo, referencia, texto, estado, orden_id=None, motivo=None):
    with conectar() as con:
        cur = con.execute(
            """INSERT INTO salientes (tipo, referencia, orden_id, texto, estado, motivo, creado)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tipo, str(referencia), orden_id, texto, estado, motivo, ahora()),
        )
        return cur.lastrowid


def obtener_saliente(saliente_id):
    with conectar() as con:
        fila = con.execute("SELECT * FROM salientes WHERE id = ?", (saliente_id,)).fetchone()
    return dict(fila) if fila else None


def actualizar_saliente(saliente_id, **campos):
    if not campos:
        return
    columnas = ", ".join(f"{k} = ?" for k in campos)
    with conectar() as con:
        con.execute(f"UPDATE salientes SET {columnas} WHERE id = ?", (*campos.values(), saliente_id))


def salientes_pendientes():
    with conectar() as con:
        return _dicts(con.execute(
            "SELECT * FROM salientes WHERE estado IN ('pendiente', 'error') ORDER BY id DESC"
        ).fetchall())


# --- preguntas --------------------------------------------------------------

def guardar_pregunta(question_id, item_id, item_titulo, comprador_id, texto):
    with conectar() as con:
        con.execute(
            """INSERT OR IGNORE INTO preguntas (id, item_id, item_titulo, comprador_id, texto, creado)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(question_id), item_id, item_titulo, str(comprador_id or ""), texto, ahora()),
        )


def obtener_pregunta(question_id):
    with conectar() as con:
        fila = con.execute("SELECT * FROM preguntas WHERE id = ?", (str(question_id),)).fetchone()
    return dict(fila) if fila else None


def ultimas_preguntas(limite=30):
    with conectar() as con:
        return _dicts(con.execute(
            """SELECT p.*, s.texto AS respuesta, s.estado AS estado_respuesta
               FROM preguntas p
               LEFT JOIN salientes s ON s.tipo = 'respuesta' AND s.referencia = p.id
               ORDER BY p.creado DESC LIMIT ?""",
            (limite,),
        ).fetchall())


# --- órdenes ---------------------------------------------------------------

def obtener_orden(orden_id):
    with conectar() as con:
        fila = con.execute("SELECT * FROM ordenes WHERE id = ?", (str(orden_id),)).fetchone()
    return dict(fila) if fila else None


def orden_por_pack(pack_id):
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM ordenes WHERE pack_id = ? ORDER BY creado LIMIT 1", (str(pack_id),)
        ).fetchone()
    return dict(fila) if fila else None


def crear_orden(orden_id, pack_id, comprador_id, apodo, nombre, detalle, total, moneda):
    """Devuelve True si la orden es nueva (evita atenderla dos veces si llegan avisos repetidos)."""
    with conectar() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO ordenes (id, pack_id, comprador_id, comprador_apodo,
                 comprador_nombre, detalle, total, moneda, estado, creado, actualizado)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nueva', ?, ?)""",
            (str(orden_id), str(pack_id), str(comprador_id), apodo, nombre, detalle,
             total, moneda, ahora(), ahora()),
        )
        return cur.rowcount == 1


def actualizar_orden(orden_id, **campos):
    if not campos:
        return
    campos["actualizado"] = ahora()
    columnas = ", ".join(f"{k} = ?" for k in campos)
    with conectar() as con:
        con.execute(f"UPDATE ordenes SET {columnas} WHERE id = ?", (*campos.values(), str(orden_id)))


def listar_ordenes(limite=50):
    with conectar() as con:
        return _dicts(con.execute(
            "SELECT * FROM ordenes ORDER BY actualizado DESC LIMIT ?", (limite,)
        ).fetchall())


# --- mensajes ---------------------------------------------------------------

def guardar_mensaje(meli_id, pack_id, rol, texto, fecha=None):
    """Devuelve True si el mensaje es nuevo."""
    with conectar() as con:
        try:
            con.execute(
                "INSERT INTO mensajes (meli_id, pack_id, rol, texto, fecha) VALUES (?, ?, ?, ?, ?)",
                (str(meli_id), str(pack_id), rol, texto, fecha or ahora()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def historial_pack(pack_id):
    with conectar() as con:
        return _dicts(con.execute(
            # Orden de llegada: las fechas de Mercado Libre y las locales vienen
            # en husos distintos, así que ordenar por texto de fecha mezclaría todo.
            "SELECT * FROM mensajes WHERE pack_id = ? ORDER BY rowid", (str(pack_id),)
        ).fetchall())


# --- pagos ------------------------------------------------------------------

def registrar_pago(orden_id, datos):
    with conectar() as con:
        cur = con.execute(
            """INSERT INTO pagos (orden_id, metodo, banco, referencia, monto, fecha_pago,
                 titular, estado, creado)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'por_verificar', ?)""",
            (str(orden_id), datos.get("metodo"), datos.get("banco"), datos.get("referencia"),
             datos.get("monto"), datos.get("fecha"), datos.get("titular"), ahora()),
        )
        return cur.lastrowid


def obtener_pago(pago_id):
    with conectar() as con:
        fila = con.execute("SELECT * FROM pagos WHERE id = ?", (pago_id,)).fetchone()
    return dict(fila) if fila else None


def actualizar_pago(pago_id, estado, nota=None):
    with conectar() as con:
        con.execute("UPDATE pagos SET estado = ?, nota = ? WHERE id = ?", (estado, nota, pago_id))


def pagos_por_verificar():
    with conectar() as con:
        return _dicts(con.execute(
            """SELECT p.*, o.comprador_apodo, o.comprador_nombre, o.detalle, o.total, o.moneda
               FROM pagos p JOIN ordenes o ON o.id = p.orden_id
               WHERE p.estado = 'por_verificar' ORDER BY p.creado"""
        ).fetchall())


# --- avisos al dueño ---------------------------------------------------------

def registrar_aviso(texto, canal):
    with conectar() as con:
        con.execute("INSERT INTO avisos (texto, canal, creado) VALUES (?, ?, ?)", (texto, canal, ahora()))


def ultimos_avisos(limite=20):
    with conectar() as con:
        return _dicts(con.execute(
            "SELECT * FROM avisos ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall())


def datos_envio(orden):
    try:
        return json.loads(orden.get("datos_envio") or "{}")
    except ValueError:
        return {}
