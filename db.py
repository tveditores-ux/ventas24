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
    wecall_ultimo_id INTEGER,                  -- último id de mensaje de WeCall ya procesado
    modo_manual INTEGER NOT NULL DEFAULT 0,    -- 1 = un humano lleva la conversación, el bot no responde solo
    asignado_a INTEGER REFERENCES usuarios(id) -- vendedor dueño de esta conversación (NULL = sin asignar)
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL DEFAULT 'vendedor',      -- 'super_admin' | 'admin' | 'vendedor'
    activo INTEGER NOT NULL DEFAULT 1,
    fecha_creacion TEXT DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS solicitudes_equipo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contacto_id INTEGER NOT NULL REFERENCES contactos(id),
    tipo TEXT NOT NULL DEFAULT 'otro',          -- condiciones / pago / existencia / otro
    asunto TEXT NOT NULL,
    detalle TEXT,
    estado TEXT NOT NULL DEFAULT 'abierta',     -- abierta / resuelta
    creada_en TEXT DEFAULT CURRENT_TIMESTAMP,
    resuelta_por INTEGER REFERENCES usuarios(id),
    resuelta_en TEXT
);

CREATE TABLE IF NOT EXISTS comprobantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id INTEGER REFERENCES pedidos(id),
    contacto_id INTEGER NOT NULL REFERENCES contactos(id),
    origen TEXT NOT NULL DEFAULT 'manual',      -- manual / whatsapp
    banco TEXT,
    referencia TEXT,
    referencia_norm TEXT,
    monto REAL,
    moneda TEXT NOT NULL DEFAULT 'USD',         -- USD / VES
    fecha_pago TEXT,                            -- YYYY-MM-DD
    cuenta_destino TEXT,
    semaforo TEXT NOT NULL DEFAULT 'amarillo',  -- verde / amarillo / rojo
    alertas TEXT NOT NULL DEFAULT '[]',         -- JSON: lista de textos
    estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente / aceptado / rechazado
    nota TEXT,
    registrado_por INTEGER REFERENCES usuarios(id),
    revisado_por INTEGER REFERENCES usuarios(id),
    revisado_en TEXT,
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
        if "modo_manual" not in columnas:
            con.execute("ALTER TABLE contactos ADD COLUMN modo_manual INTEGER NOT NULL DEFAULT 0")
        if "asignado_a" not in columnas:
            con.execute("ALTER TABLE contactos ADD COLUMN asignado_a INTEGER REFERENCES usuarios(id)")
        cols_pedidos = {f["name"] for f in con.execute("PRAGMA table_info(pedidos)")}
        for col, ddl in (
            ("estado_pago", "TEXT NOT NULL DEFAULT 'por_pagar'"),
            ("pago_confirmado_por", "INTEGER REFERENCES usuarios(id)"),
            ("pago_confirmado_en", "TEXT"),
            ("despachado_por", "INTEGER REFERENCES usuarios(id)"),
            ("despachado_en", "TEXT"),
            ("orden", "TEXT"),            # agrupa las líneas de un mismo pedido
            ("nota_despacho", "TEXT"),
        ):
            if col not in cols_pedidos:
                con.execute(f"ALTER TABLE pedidos ADD COLUMN {col} {ddl}")
        cols_catalogo = {f["name"] for f in con.execute("PRAGMA table_info(catalogo)")}
        for col, ddl in (
            ("codigo", "TEXT"),
            ("precio_mayor", "REAL"),                       # NULL = usa `precio` para todos
            ("stock_verificado", "INTEGER NOT NULL DEFAULT 1"),  # 0 = existencia por confirmar
        ):
            if col not in cols_catalogo:
                con.execute(f"ALTER TABLE catalogo ADD COLUMN {col} {ddl}")
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


def listar_contactos_con_actividad(usuario: dict | None = None):
    """Contactos que ya tuvieron alguna conversación (WeCall o Twilio),
    con su último mensaje, para la vista de conversaciones del CRM.

    Si `usuario` es un vendedor, solo devuelve lo suyo (asignado_a él) o
    lo que todavía no tiene dueño (para que pueda "tomarlo"). Admin y
    super_admin ven todo — se les pasa `usuario=None` o con otro rol."""
    where_rol = ""
    params = []
    if usuario and usuario["rol"] == "vendedor":
        where_rol = " AND (contactos.asignado_a = ? OR contactos.asignado_a IS NULL)"
        params.append(usuario["id"])

    with conectar() as con:
        filas = con.execute(f"""
            SELECT
                contactos.id, contactos.telefono, contactos.nombre, contactos.tipo,
                contactos.estado, contactos.modo_manual, contactos.fecha_ultimo_contacto,
                contactos.wecall_ultimo_id, contactos.asignado_a,
                usuarios.nombre AS asignado_nombre,
                (SELECT mensaje FROM interacciones WHERE contacto_id = contactos.id ORDER BY id DESC LIMIT 1) AS ultimo_mensaje,
                (SELECT rol FROM interacciones WHERE contacto_id = contactos.id ORDER BY id DESC LIMIT 1) AS ultimo_rol
            FROM contactos
            LEFT JOIN usuarios ON usuarios.id = contactos.asignado_a
            WHERE (wecall_ultimo_id IS NOT NULL
               OR EXISTS (SELECT 1 FROM interacciones WHERE contacto_id = contactos.id))
              {where_rol}
            ORDER BY fecha_ultimo_contacto DESC
        """, params).fetchall()
        return [dict(f) for f in filas]


def asignar_conversacion_si_libre(contacto_id: int, usuario_id: int) -> bool:
    """Asigna la conversación a este usuario SOLO si no tenía dueño todavía
    (primero que la atiende se la queda). Devuelve True si quedó asignada
    a este usuario (ya sea recién o de antes), False si es de otro."""
    with conectar() as con:
        fila = con.execute("SELECT asignado_a FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        if fila is None:
            return False
        if fila["asignado_a"] is None:
            con.execute("UPDATE contactos SET asignado_a = ? WHERE id = ?", (usuario_id, contacto_id))
            return True
        return fila["asignado_a"] == usuario_id


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

def registrar_pedido(contacto_id: int, producto: str, cantidad: int, precio_unitario: float,
                     estado_pago: str = "por_confirmar", orden: str | None = None):
    """Todo pedido nace 'por_confirmar': una persona verifica la existencia antes de cobrar."""
    total = round(cantidad * precio_unitario, 2)
    with conectar() as con:
        con.execute(
            "INSERT INTO pedidos (contacto_id, producto, cantidad, precio_unitario, total, estado_pago, orden) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contacto_id, producto, cantidad, precio_unitario, total, estado_pago, orden),
        )
    return total


def listar_pedidos(contacto_id: int | None = None, usuario: dict | None = None):
    """Si `usuario` es un vendedor, solo pedidos de contactos asignados a él."""
    where_rol = ""
    params = []
    if contacto_id:
        where_rol = " WHERE pedidos.contacto_id = ?"
        params.append(contacto_id)
    elif usuario and usuario["rol"] == "vendedor":
        where_rol = " WHERE contactos.asignado_a = ?"
        params.append(usuario["id"])

    with conectar() as con:
        filas = con.execute(f"""
            SELECT pedidos.*, contactos.nombre, contactos.telefono
            FROM pedidos JOIN contactos ON contactos.id = pedidos.contacto_id
            {where_rol}
            ORDER BY pedidos.fecha DESC
        """, params).fetchall()
        return [dict(f) for f in filas]


def lineas_de_orden(pedido_id: int):
    """Todas las líneas de la orden a la que pertenece este pedido (o solo él si no tiene orden)."""
    with conectar() as con:
        fila = con.execute("SELECT orden FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
        if not fila:
            return []
        if fila["orden"]:
            filas = con.execute("SELECT * FROM pedidos WHERE orden = ? ORDER BY id", (fila["orden"],)).fetchall()
        else:
            filas = con.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchall()
        return [dict(f) for f in filas]


def obtener_pedido(pedido_id: int):
    with conectar() as con:
        fila = con.execute(
            "SELECT pedidos.*, contactos.nombre, contactos.telefono, contactos.asignado_a "
            "FROM pedidos JOIN contactos ON contactos.id = pedidos.contacto_id WHERE pedidos.id = ?",
            (pedido_id,),
        ).fetchone()
    if not fila:
        return None
    pedido = dict(fila)
    pedido["total_orden"] = round(sum(l["total"] or 0 for l in lineas_de_orden(pedido_id)), 2)
    return pedido


def cambiar_estado_pago_orden(pedido_id: int, desde: tuple, hacia: str):
    """Mueve todas las líneas de la orden que estén en alguno de los estados `desde`. Devuelve cuántas."""
    ids = [l["id"] for l in lineas_de_orden(pedido_id) if l["estado_pago"] in desde and l["estado"] != "cancelado"]
    if not ids:
        return 0
    with conectar() as con:
        con.execute(
            f"UPDATE pedidos SET estado_pago = ? WHERE id IN ({','.join('?' * len(ids))})", (hacia, *ids)
        )
    return len(ids)


def cancelar_orden(pedido_id: int):
    ids = [l["id"] for l in lineas_de_orden(pedido_id) if l["estado"] not in ("entregado", "cancelado")]
    if ids:
        with conectar() as con:
            con.execute(f"UPDATE pedidos SET estado = 'cancelado' WHERE id IN ({','.join('?' * len(ids))})", ids)
    return len(ids)


def pedido_por_pagar_reciente(contacto_id: int):
    """Último pedido del contacto que todavía espera pago (para atar un adjunto entrante)."""
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM pedidos WHERE contacto_id = ? AND estado != 'cancelado' "
            "AND estado_pago IN ('por_confirmar', 'por_pagar', 'comprobante_recibido') ORDER BY id DESC LIMIT 1",
            (contacto_id,),
        ).fetchone()
        return dict(fila) if fila else None


def despachar_pedido(pedido_id: int, usuario_id: int, nota: str | None = None):
    """Compuerta de envío: solo se despacha una orden con el pago confirmado
    por un administrador. Aplica a todas sus líneas. Devuelve (ok, motivo)."""
    lineas = [l for l in lineas_de_orden(pedido_id) if l["estado"] != "cancelado"]
    if not lineas:
        return False, "pedido no encontrado o cancelado"
    if all(l["estado"] == "entregado" for l in lineas):
        return False, "el pedido ya fue despachado"
    if any(l["estado_pago"] != "pago_confirmado" for l in lineas):
        return False, "el pago todavía no está confirmado por la administración"
    ids = [l["id"] for l in lineas if l["estado"] != "entregado"]
    with conectar() as con:
        con.execute(
            f"UPDATE pedidos SET estado = 'entregado', despachado_por = ?, despachado_en = CURRENT_TIMESTAMP, "
            f"nota_despacho = ? WHERE id IN ({','.join('?' * len(ids))})",
            (usuario_id, nota, *ids),
        )
    return True, "despachado"


# ---------------------------------------------------------------------
# Comprobantes de pago
# ---------------------------------------------------------------------

def crear_comprobante(contacto_id: int, pedido_id, origen: str, datos: dict,
                      semaforo: str, alertas: list, registrado_por=None) -> int:
    import json
    with conectar() as con:
        cur = con.execute(
            """INSERT INTO comprobantes
               (pedido_id, contacto_id, origen, banco, referencia, referencia_norm, monto, moneda,
                fecha_pago, cuenta_destino, semaforo, alertas, nota, registrado_por)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pedido_id, contacto_id, origen, datos.get("banco"), datos.get("referencia"),
             datos.get("referencia_norm"), datos.get("monto"), datos.get("moneda") or "USD",
             datos.get("fecha_pago"), datos.get("cuenta_destino"), semaforo,
             json.dumps(alertas, ensure_ascii=False), datos.get("nota"), registrado_por),
        )
        comprobante_id = cur.lastrowid
    if pedido_id:
        cambiar_estado_pago_orden(pedido_id, ("por_pagar",), "comprobante_recibido")
    return comprobante_id


def actualizar_datos_comprobante(comprobante_id: int, datos: dict, semaforo: str, alertas: list):
    import json
    with conectar() as con:
        con.execute(
            """UPDATE comprobantes SET banco = ?, referencia = ?, referencia_norm = ?, monto = ?,
               moneda = ?, fecha_pago = ?, cuenta_destino = ?, nota = COALESCE(?, nota),
               semaforo = ?, alertas = ? WHERE id = ?""",
            (datos.get("banco"), datos.get("referencia"), datos.get("referencia_norm"), datos.get("monto"),
             datos.get("moneda") or "USD", datos.get("fecha_pago"), datos.get("cuenta_destino"),
             datos.get("nota"), semaforo, json.dumps(alertas, ensure_ascii=False), comprobante_id),
        )


def obtener_comprobante(comprobante_id: int):
    with conectar() as con:
        fila = con.execute(
            """SELECT comprobantes.*, contactos.nombre, contactos.telefono, contactos.asignado_a,
                      pedidos.total AS pedido_total, pedidos.producto AS pedido_producto,
                      pedidos.fecha AS pedido_fecha
               FROM comprobantes
               JOIN contactos ON contactos.id = comprobantes.contacto_id
               LEFT JOIN pedidos ON pedidos.id = comprobantes.pedido_id
               WHERE comprobantes.id = ?""",
            (comprobante_id,),
        ).fetchone()
        return dict(fila) if fila else None


def listar_comprobantes(usuario: dict | None = None):
    import json
    donde, params = "", []
    if usuario and usuario["rol"] == "vendedor":
        donde = " WHERE contactos.asignado_a = ?"
        params.append(usuario["id"])
    with conectar() as con:
        filas = con.execute(f"""
            SELECT comprobantes.*, contactos.nombre, contactos.telefono,
                   pedidos.total AS pedido_total, pedidos.producto AS pedido_producto,
                   revisor.nombre AS revisado_por_nombre
            FROM comprobantes
            JOIN contactos ON contactos.id = comprobantes.contacto_id
            LEFT JOIN pedidos ON pedidos.id = comprobantes.pedido_id
            LEFT JOIN usuarios revisor ON revisor.id = comprobantes.revisado_por
            {donde}
            ORDER BY (comprobantes.estado = 'pendiente') DESC, comprobantes.id DESC
        """, params).fetchall()
    resultado = []
    for f in filas:
        d = dict(f)
        d["alertas"] = json.loads(d.get("alertas") or "[]")
        resultado.append(d)
    return resultado


def referencias_usadas(excluir_comprobante_id=None):
    """(comprobante_id, pedido_id, referencia_norm) de todos los comprobantes con referencia."""
    with conectar() as con:
        filas = con.execute(
            "SELECT id, pedido_id, referencia_norm FROM comprobantes "
            "WHERE referencia_norm IS NOT NULL AND referencia_norm != '' AND id != ?",
            (excluir_comprobante_id or -1,),
        ).fetchall()
        return [(f["id"], f["pedido_id"], f["referencia_norm"]) for f in filas]


def revisar_comprobante(comprobante_id: int, usuario_id: int, aceptar: bool, nota: str | None):
    """Decisión de la administración. Aceptar confirma el pago del pedido;
    rechazar lo devuelve a 'por_pagar' si no queda otro comprobante aceptado."""
    with conectar() as con:
        comp = con.execute("SELECT pedido_id, estado FROM comprobantes WHERE id = ?", (comprobante_id,)).fetchone()
        if not comp:
            return False, "comprobante no encontrado"
        if comp["estado"] != "pendiente":
            return False, "este comprobante ya fue revisado"
        con.execute(
            "UPDATE comprobantes SET estado = ?, revisado_por = ?, revisado_en = CURRENT_TIMESTAMP, "
            "nota = COALESCE(?, nota) WHERE id = ?",
            ("aceptado" if aceptar else "rechazado", usuario_id, nota, comprobante_id),
        )
        pid = comp["pedido_id"]
        if pid and aceptar:
            ids = [l["id"] for l in lineas_de_orden(pid) if l["estado"] != "cancelado"]
            con.execute(
                "UPDATE pedidos SET estado_pago = 'pago_confirmado', estado = CASE WHEN estado = 'pendiente' "
                "THEN 'confirmado' ELSE estado END, pago_confirmado_por = ?, pago_confirmado_en = CURRENT_TIMESTAMP "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                (usuario_id, *ids),
            )
        elif pid:
            quedan = con.execute(
                "SELECT COUNT(*) AS n FROM comprobantes WHERE pedido_id = ? AND estado IN ('pendiente', 'aceptado')",
                (pid,),
            ).fetchone()["n"]
            if not quedan:
                ids = [l["id"] for l in lineas_de_orden(pid) if l["estado_pago"] == "comprobante_recibido"]
                if ids:
                    con.execute(
                        f"UPDATE pedidos SET estado_pago = 'por_pagar' WHERE id IN ({','.join('?' * len(ids))})", ids
                    )
    return True, "ok"


# ---------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------

def listar_catalogo_completo():
    """Todo el catálogo con id, para la vista del dashboard (no la que usa el agente)."""
    with conectar() as con:
        filas = con.execute("SELECT * FROM catalogo ORDER BY marca, modelo, nombre").fetchall()
        return [dict(f) for f in filas]


_ALIAS_VEHICULO = {
    "toyota": "toy", "chevrolet": "chev", "hyundai": "hyu", "nissan": "nis", "mitsubishi": "mit",
    "honda": "hon", "mazda": "maz", "renault": "ren", "dodge": "dod", "volkswagen": "vw",
    "daewoo": "dae", "isuzu": "isu", "suzuki": "suz", "peugeot": "peu",
}
_PALABRAS_VACIAS = {"de", "del", "la", "el", "los", "las", "para", "y", "un", "una", "con", "en"}


def _normalizar(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def precio_para(fila, tipo: str | None):
    """Mayoristas pagan `precio_mayor` si el producto lo tiene; el resto, `precio`."""
    mayor = fila["precio_mayor"] if "precio_mayor" in fila.keys() else None
    return mayor if (tipo == "mayorista" and mayor) else fila["precio"]


def buscar_catalogo(marca=None, modelo=None, anio=None, nombre=None, texto=None, tipo=None, limite=15):
    with conectar() as con:
        productos = con.execute("SELECT * FROM catalogo").fetchall()

    # Palabras clave para los productos de la lista (descripción libre).
    crudas = " ".join(str(x) for x in (texto, marca, modelo, nombre) if x)
    tokens = [t for t in _normalizar(crudas).replace("/", " ").replace(",", " ").split() if t not in _PALABRAS_VACIAS]

    resultados, de_lista = [], []
    for p in productos:
        if p["codigo"]:
            if not tokens:
                continue
            pajar = _normalizar(f'{p["nombre"]} {p["codigo"]} {p["marca"]}')
            if all(_coincide(t, pajar) for t in tokens):
                de_lista.append(p)
            continue

        marca_ok = not marca or p["marca"].lower() == marca.lower() or p["marca"] == "Universal"
        modelo_ok = not modelo or p["modelo"].lower() == modelo.lower() or p["modelo"] == "Universal"
        anio_ok = (
            not anio
            or p["anio_desde"] is None
            or (p["anio_desde"] <= anio <= p["anio_hasta"])
        )
        nombre_ok = not nombre or nombre.lower() in p["nombre"].lower()

        if marca_ok and modelo_ok and anio_ok and nombre_ok:
            resultados.append(_ficha(p, tipo))

    # Lo más corto primero: suele ser la descripción más genérica del producto.
    primero = tokens[0] if tokens else ""
    de_lista.sort(key=lambda p: (not _normalizar(p["nombre"]).startswith(primero), len(p["nombre"])))
    resultados.extend(_ficha(p, tipo) for p in de_lista[:limite])
    return resultados


def _coincide(token, pajar):
    alias = _ALIAS_VEHICULO.get(token)
    return token in pajar or (alias is not None and alias in pajar.split())


def _ficha(p, tipo):
    return {
        "codigo": p["codigo"],
        "nombre": p["nombre"],
        "marca": p["marca"],
        "modelo": p["modelo"],
        "precio_usd": precio_para(p, tipo),
        "stock": p["stock"] if p["stock_verificado"] else "por confirmar",
    }


def reemplazar_catalogo(filas: list, stock: int = 999):
    """Respalda el catálogo actual en `catalogo_respaldo` y lo reemplaza por `filas`
    (dicts con codigo, nombre, marca, precio, precio_mayor). Todo en una transacción."""
    with conectar() as con:
        con.execute("DROP TABLE IF EXISTS catalogo_respaldo")
        con.execute("CREATE TABLE catalogo_respaldo AS SELECT * FROM catalogo")
        con.execute("DELETE FROM catalogo")
        con.executemany(
            "INSERT INTO catalogo (codigo, nombre, marca, modelo, precio, precio_mayor, stock, stock_verificado) "
            "VALUES (?, ?, ?, '', ?, ?, ?, 0)",
            [(f["codigo"], f["nombre"], f["marca"], f["precio"], f["precio_mayor"], stock) for f in filas],
        )
    return len(filas)


def insertar_producto(nombre, marca, modelo, anio_desde, anio_hasta, precio, stock):
    with conectar() as con:
        con.execute(
            "INSERT INTO catalogo (nombre, marca, modelo, anio_desde, anio_hasta, precio, stock) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nombre, marca, modelo, anio_desde, anio_hasta, precio, stock),
        )


# ---------------------------------------------------------------------
# Usuarios (dashboard con roles: super_admin / admin / vendedor)
# ---------------------------------------------------------------------

ROLES_VALIDOS = ("super_admin", "admin", "vendedor")


def crear_usuario(nombre: str, email: str, password_hash: str, rol: str = "vendedor") -> int:
    if rol not in ROLES_VALIDOS:
        raise ValueError(f"rol inválido: {rol}")
    with conectar() as con:
        cur = con.execute(
            "INSERT INTO usuarios (nombre, email, password_hash, rol) VALUES (?, ?, ?, ?)",
            (nombre, email.strip().lower(), password_hash, rol),
        )
        return cur.lastrowid


def obtener_usuario_por_email(email: str):
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM usuarios WHERE email = ? AND activo = 1", (email.strip().lower(),)
        ).fetchone()
        return dict(fila) if fila else None


def obtener_usuario(usuario_id: int):
    with conectar() as con:
        fila = con.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        return dict(fila) if fila else None


def listar_usuarios():
    with conectar() as con:
        filas = con.execute("SELECT id, nombre, email, rol, activo, fecha_creacion FROM usuarios ORDER BY fecha_creacion").fetchall()
        return [dict(f) for f in filas]


def actualizar_usuario(usuario_id: int, **campos):
    if not campos:
        return
    columnas = ", ".join(f"{k} = ?" for k in campos)
    valores = list(campos.values()) + [usuario_id]
    with conectar() as con:
        con.execute(f"UPDATE usuarios SET {columnas} WHERE id = ?", valores)


inicializar()


# ---------------------------------------------------------------------
# Solicitudes al equipo y pendientes
# ---------------------------------------------------------------------

def crear_solicitud(contacto_id: int, tipo: str, asunto: str, detalle: str | None = None) -> int:
    """Pide algo a una persona del equipo sin apagar al bot. Si ya hay una abierta
    del mismo tipo para este contacto, la actualiza en vez de duplicarla."""
    with conectar() as con:
        previa = con.execute(
            "SELECT id FROM solicitudes_equipo WHERE contacto_id = ? AND tipo = ? AND estado = 'abierta'",
            (contacto_id, tipo),
        ).fetchone()
        if previa:
            con.execute(
                "UPDATE solicitudes_equipo SET asunto = ?, detalle = ? WHERE id = ?", (asunto, detalle, previa["id"])
            )
            return previa["id"]
        cur = con.execute(
            "INSERT INTO solicitudes_equipo (contacto_id, tipo, asunto, detalle) VALUES (?, ?, ?, ?)",
            (contacto_id, tipo, asunto, detalle),
        )
        return cur.lastrowid


def _filtro_vendedor(usuario, columna="contactos.asignado_a"):
    if usuario and usuario["rol"] == "vendedor":
        return f" AND ({columna} IS NULL OR {columna} = ?)", [usuario["id"]]
    return "", []


def listar_solicitudes(usuario: dict | None = None):
    extra, params = _filtro_vendedor(usuario)
    with conectar() as con:
        filas = con.execute(
            "SELECT s.*, contactos.nombre, contactos.telefono FROM solicitudes_equipo s "
            "JOIN contactos ON contactos.id = s.contacto_id "
            f"WHERE s.estado = 'abierta'{extra} ORDER BY s.id DESC",
            params,
        ).fetchall()
        return [dict(f) for f in filas]


def resolver_solicitud(solicitud_id: int, usuario_id: int) -> bool:
    with conectar() as con:
        cur = con.execute(
            "UPDATE solicitudes_equipo SET estado = 'resuelta', resuelta_por = ?, resuelta_en = CURRENT_TIMESTAMP "
            "WHERE id = ? AND estado = 'abierta'",
            (usuario_id, solicitud_id),
        )
        return cur.rowcount > 0


def listar_derivadas(usuario: dict | None = None):
    """Conversaciones que el bot pasó a una persona (modo manual activado)."""
    extra, params = _filtro_vendedor(usuario)
    with conectar() as con:
        filas = con.execute(
            "SELECT id, nombre, telefono, notas, fecha_ultimo_contacto FROM contactos "
            f"WHERE modo_manual = 1{extra} ORDER BY fecha_ultimo_contacto DESC",
            params,
        ).fetchall()
        return [dict(f) for f in filas]


def contar_pendientes(usuario: dict | None = None) -> dict:
    extra, params = _filtro_vendedor(usuario)
    with conectar() as con:
        por_confirmar = con.execute(
            "SELECT COUNT(DISTINCT COALESCE(pedidos.orden, 'p' || pedidos.id)) AS n FROM pedidos "
            "JOIN contactos ON contactos.id = pedidos.contacto_id "
            f"WHERE pedidos.estado_pago = 'por_confirmar' AND pedidos.estado != 'cancelado'{extra}",
            params,
        ).fetchone()["n"]
        comprobantes = con.execute(
            "SELECT COUNT(*) AS n FROM comprobantes JOIN contactos ON contactos.id = comprobantes.contacto_id "
            f"WHERE comprobantes.estado = 'pendiente'{extra}",
            params,
        ).fetchone()["n"]
    return {
        "existencia_por_confirmar": por_confirmar,
        "comprobantes_pendientes": comprobantes,
        "solicitudes_abiertas": len(listar_solicitudes(usuario)),
        "conversaciones_derivadas": len(listar_derivadas(usuario)),
    }
