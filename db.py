"""
Base de datos del CRM (SQLite). Reemplaza los CSVs sueltos: todo lo que
antes vivía en catalogo_50_modelos_venezuela.csv, lista_espera.csv y
clientes_mayoristas.csv, más el historial de conversación (antes solo en
memoria), ahora vive acá, conectado por contacto.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# En producción (Render), CRM_DB_PATH apunta al disco persistente
# (ej. /var/data/crm.db) para que la base sobreviva a cada deploy.
RUTA_DB = os.environ.get("CRM_DB_PATH") or os.path.join(os.path.dirname(__file__), "crm.db")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS contactos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefono TEXT UNIQUE NOT NULL,
    nombre TEXT,
    tipo TEXT NOT NULL DEFAULT 'cliente',      -- 'cliente' | 'mayorista'
    negocio TEXT,                              -- solo mayoristas
    tipo_negocio TEXT,                         -- solo mayoristas
    ciudad TEXT,
    estado TEXT NOT NULL DEFAULT 'nuevo',      -- nuevo/contactado/interesado/no_interesado/cliente
    notas TEXT DEFAULT '',
    fecha_creacion TEXT DEFAULT CURRENT_TIMESTAMP,
    fecha_ultimo_contacto TEXT,
    wecall_ultimo_id INTEGER                   -- último id de mensaje de WeCall ya procesado
);

CREATE TABLE IF NOT EXISTS interacciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contacto_id INTEGER NOT NULL REFERENCES contactos(id),
    rol TEXT NOT NULL,        -- 'user' | 'assistant'
    mensaje TEXT NOT NULL,
    fecha TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS solicitudes_espera (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contacto_id INTEGER NOT NULL REFERENCES contactos(id),
    producto TEXT NOT NULL,
    atendido INTEGER NOT NULL DEFAULT 0,
    fecha TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contacto_id INTEGER NOT NULL REFERENCES contactos(id),
    producto TEXT NOT NULL,
    cantidad INTEGER NOT NULL DEFAULT 1,
    precio_unitario REAL DEFAULT 0,
    total REAL DEFAULT 0,
    estado TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente/confirmado/entregado/cancelado
    fecha TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalogo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    marca TEXT NOT NULL,
    modelo TEXT NOT NULL,
    anio_desde INTEGER,
    anio_hasta INTEGER,
    precio REAL NOT NULL DEFAULT 0,
    stock INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def conectar():
    con = sqlite3.connect(RUTA_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def inicializar():
    with conectar() as con:
        con.executescript(ESQUEMA)
        # Migración liviana para bases creadas antes de agregar esta columna.
        columnas = {f["name"] for f in con.execute("PRAGMA table_info(contactos)")}
        if "wecall_ultimo_id" not in columnas:
            con.execute("ALTER TABLE contactos ADD COLUMN wecall_ultimo_id INTEGER")
        # Los contactos migrados desde Twilio guardaban el prefijo "whatsapp:".
        # Se normaliza acá para que todos los canales compartan el mismo formato E.164.
        con.execute(
            "UPDATE contactos SET telefono = substr(telefono, 10) "
            "WHERE telefono LIKE 'whatsapp:%'"
        )


def normalizar_telefono(telefono: str) -> str:
    """Formato E.164 (+58...) sin importar si viene con el prefijo whatsapp: de Twilio."""
    if not telefono:
        return telefono
    t = telefono.strip()
    if t.startswith("whatsapp:"):
        t = t[len("whatsapp:"):]
    return t


# ---------------------------------------------------------------------
# Contactos
# ---------------------------------------------------------------------

def obtener_o_crear_contacto(telefono: str, tipo: str = "cliente") -> int:
    """Devuelve el id del contacto para ese teléfono, creándolo si no existe."""
    telefono = normalizar_telefono(telefono)
    with conectar() as con:
        fila = con.execute("SELECT id FROM contactos WHERE telefono = ?", (telefono,)).fetchone()
        if fila:
            return fila["id"]
        cur = con.execute(
            "INSERT INTO contactos (telefono, tipo, fecha_ultimo_contacto) VALUES (?, ?, ?)",
            (telefono, tipo, datetime.now().isoformat(timespec="minutes")),
        )
        return cur.lastrowid


def actualizar_contacto(contacto_id: int, **campos):
    """Actualiza cualquier combinación de columnas de contactos (nombre, estado, notas, etc.)."""
    if not campos:
        return
    columnas = ", ".join(f"{k} = ?" for k in campos)
    valores = list(campos.values()) + [contacto_id]
    with conectar() as con:
        con.execute(f"UPDATE contactos SET {columnas} WHERE id = ?", valores)


def tocar_ultimo_contacto(contacto_id: int):
    actualizar_contacto(contacto_id, fecha_ultimo_contacto=datetime.now().isoformat(timespec="minutes"))


def listar_contactos_wecall():
    """Contactos que ya tuvieron al menos un mensaje de WeCall — para el polling de respaldo."""
    with conectar() as con:
        filas = con.execute(
            "SELECT id, telefono, wecall_ultimo_id FROM contactos WHERE wecall_ultimo_id IS NOT NULL"
        ).fetchall()
        return [dict(f) for f in filas]


def listar_mayoristas(estado: str | None = None):
    with conectar() as con:
        if estado:
            filas = con.execute(
                "SELECT * FROM contactos WHERE tipo = 'mayorista' AND estado = ? ORDER BY fecha_creacion",
                (estado,),
            ).fetchall()
        else:
            filas = con.execute(
                "SELECT * FROM contactos WHERE tipo = 'mayorista' ORDER BY fecha_creacion"
            ).fetchall()
        return [dict(f) for f in filas]


# ---------------------------------------------------------------------
# Historial de conversación
# ---------------------------------------------------------------------

def registrar_interaccion(contacto_id: int, rol: str, mensaje: str):
    with conectar() as con:
        con.execute(
            "INSERT INTO interacciones (contacto_id, rol, mensaje) VALUES (?, ?, ?)",
            (contacto_id, rol, mensaje),
        )


def obtener_historial(contacto_id: int):
    """Historial en el formato {role, content} que espera la API de Anthropic."""
    with conectar() as con:
        filas = con.execute(
            "SELECT rol, mensaje FROM interacciones WHERE contacto_id = ? ORDER BY id",
            (contacto_id,),
        ).fetchall()
        return [{"role": f["rol"], "content": f["mensaje"]} for f in filas]


# ---------------------------------------------------------------------
# Lista de espera
# ---------------------------------------------------------------------

def registrar_espera(contacto_id: int, producto: str):
    with conectar() as con:
        con.execute(
            "INSERT INTO solicitudes_espera (contacto_id, producto) VALUES (?, ?)",
            (contacto_id, producto),
        )


def listar_espera(solo_pendientes: bool = True):
    with conectar() as con:
        query = """
            SELECT solicitudes_espera.*, contactos.nombre, contactos.telefono
            FROM solicitudes_espera
            JOIN contactos ON contactos.id = solicitudes_espera.contacto_id
        """
        if solo_pendientes:
            query += " WHERE solicitudes_espera.atendido = 0"
        query += " ORDER BY solicitudes_espera.fecha DESC"
        return [dict(f) for f in con.execute(query).fetchall()]


# ---------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------

def registrar_pedido(contacto_id: int, producto: str, cantidad: int, precio_unitario: float):
    total = round(cantidad * precio_unitario, 2)
    with conectar() as con:
        con.execute(
            "INSERT INTO pedidos (contacto_id, producto, cantidad, precio_unitario, total) VALUES (?, ?, ?, ?, ?)",
            (contacto_id, producto, cantidad, precio_unitario, total),
        )
    return total


def listar_pedidos(contacto_id: int | None = None):
    with conectar() as con:
        if contacto_id:
            filas = con.execute("SELECT * FROM pedidos WHERE contacto_id = ? ORDER BY fecha DESC", (contacto_id,)).fetchall()
        else:
            filas = con.execute("""
                SELECT pedidos.*, contactos.nombre, contactos.telefono
                FROM pedidos JOIN contactos ON contactos.id = pedidos.contacto_id
                ORDER BY pedidos.fecha DESC
            """).fetchall()
        return [dict(f) for f in filas]


# ---------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------

def buscar_catalogo(marca=None, modelo=None, anio=None, nombre=None):
    with conectar() as con:
        productos = con.execute("SELECT * FROM catalogo").fetchall()

    resultados = []
    for p in productos:
        marca_ok = not marca or p["marca"].lower() == marca.lower() or p["marca"] == "Universal"
        modelo_ok = not modelo or p["modelo"].lower() == modelo.lower() or p["modelo"] == "Universal"
        anio_ok = (
            not anio
            or p["anio_desde"] is None
            or (p["anio_desde"] <= anio <= p["anio_hasta"])
        )
        nombre_ok = not nombre or nombre.lower() in p["nombre"].lower()

        if marca_ok and modelo_ok and anio_ok and nombre_ok:
            resultados.append({
                "nombre": p["nombre"],
                "marca": p["marca"],
                "modelo": p["modelo"],
                "precio_usd": p["precio"],
                "stock": p["stock"],
            })

    return resultados


def insertar_producto(nombre, marca, modelo, anio_desde, anio_hasta, precio, stock):
    with conectar() as con:
        con.execute(
            "INSERT INTO catalogo (nombre, marca, modelo, anio_desde, anio_hasta, precio, stock) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nombre, marca, modelo, anio_desde, anio_hasta, precio, stock),
        )


inicializar()
