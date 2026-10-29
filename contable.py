"""
Reportes y soportes para la contadora.

El ERP no emite libros contables: entrega reportes ordenados (ventas, compras y gastos, inventario, ingresos, bancos y caja
chica) y la carpeta mensual de soportes. Cada registro dice si tiene SOPORTE FISCAL (factura con número de control) o no;
la contadora decide qué se declara.

Las notas de entrega son una excepción controlada: exigen un motivo, las aprueba un administrador y el reporte de ventas
muestra qué porcentaje del total representan, con una alerta si pasa del límite configurado (nota_entrega_alerta_pct, 10 %).
Una nota de despacho es distinta: se convierte en factura cuando se emite.
"""

import csv
import io
import os
import re
import uuid
import zipfile
from datetime import date, datetime

import db

ESQUEMA = """
CREATE TABLE IF NOT EXISTS erp_documentos_venta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_ref INTEGER NOT NULL,                   -- id de la primera línea de la orden
    tipo TEXT NOT NULL,                            -- factura / nota_despacho / nota_entrega
    numero TEXT, control TEXT,
    fecha TEXT NOT NULL,
    motivo TEXT,                                   -- obligatorio en la nota de entrega
    registrado_por INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dv_control ON erp_documentos_venta(control) WHERE control IS NOT NULL AND control != '';

CREATE TABLE IF NOT EXISTS erp_compras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    proveedor TEXT NOT NULL, rif TEXT,
    tipo TEXT NOT NULL DEFAULT 'gasto',            -- mercancia / servicio / gasto
    doc_tipo TEXT NOT NULL DEFAULT 'factura',      -- factura / ticket / recibo / otro
    numero TEXT, control TEXT,
    moneda TEXT NOT NULL DEFAULT 'VES',            -- USD / VES
    monto REAL NOT NULL, base REAL, iva REAL, ret_iva REAL, ret_islr REAL,
    tasa REAL, monto_usd REAL, monto_bs REAL,
    forma_pago TEXT NOT NULL DEFAULT 'pendiente',  -- banco / caja_chica / pendiente
    nota TEXT,
    registrado_por INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_bancos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    cuenta TEXT NOT NULL,                          -- nombre de la cuenta, o "Caja chica"
    tipo TEXT NOT NULL,                            -- ingreso / egreso
    concepto TEXT NOT NULL,
    moneda TEXT NOT NULL DEFAULT 'VES',
    monto REAL NOT NULL, tasa REAL, monto_usd REAL, monto_bs REAL,
    referencia TEXT,
    compra_id INTEGER REFERENCES erp_compras(id),
    registrado_por INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_adjuntos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entidad TEXT NOT NULL,                         -- venta / compra / banco / pago
    entidad_id INTEGER NOT NULL,
    nombre TEXT NOT NULL, archivo TEXT NOT NULL, mime TEXT, bytes INTEGER,
    subido_por INTEGER REFERENCES usuarios(id),
    fecha TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

ENTIDADES = ("venta", "compra", "banco", "pago")
MIMES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_ADJUNTO = 10 * 1024 * 1024
LOCAL = "-4 hours"      # Venezuela: UTC-4, sin horario de verano


def asegurar_tablas():
    with db.conectar() as con:
        con.executescript(ESQUEMA)


def _fecha(f) -> str:
    try:
        return datetime.strptime(str(f)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("la fecha debe ser AAAA-MM-DD")


def _num(x, nombre="monto", obligatorio=False):
    if x in (None, ""):
        if obligatorio:
            raise ValueError(f"falta {nombre}")
        return None
    try:
        v = float(str(x).replace(",", "."))
    except ValueError:
        raise ValueError(f"{nombre} debe ser un número")
    if v < 0:
        raise ValueError(f"{nombre} no puede ser negativo")
    return v


def _montos(moneda: str, monto: float, tasa):
    tasa = tasa or (db.tasa_vigente()["tasa"] if db.tasa_vigente()["vigente"] else None)
    if not tasa:
        return None, None, None
    return (round(monto / tasa, 2), round(monto, 2), tasa) if moneda == "VES" else (round(monto, 2), round(monto * tasa, 2), tasa)


def _rango(desde, hasta):
    return desde or "0000-01-01", hasta or "9999-12-31"


# ---------------------------------------------------------------- ventas y documentos
def registrar_doc_venta(pedido_id: int, tipo: str, numero, control, fecha, motivo, usuario_id: int) -> int:
    asegurar_tablas()
    if tipo not in ("factura", "nota_despacho", "nota_entrega"):
        raise ValueError("tipo inválido")
    lineas = [l for l in db.lineas_de_orden(pedido_id) if l["estado"] != "cancelado"]
    if not lineas:
        raise ValueError("pedido no encontrado o cancelado")
    ref = lineas[0]["id"]
    numero, control, motivo = (numero or "").strip() or None, (control or "").strip() or None, (motivo or "").strip() or None
    if tipo == "factura" and not (numero and control):
        raise ValueError("la factura necesita su número y su número de control")
    if tipo == "nota_entrega" and (not motivo or len(motivo) < 10):
        raise ValueError("la nota de entrega es una excepción: escribe el motivo (mínimo 10 caracteres)")
    with db.conectar() as con:
        previo = con.execute("SELECT id, tipo FROM erp_documentos_venta WHERE pedido_ref = ? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
        if previo:
            raise ValueError("esta orden ya tiene un documento" + ("; conviértelo en factura con su número y control" if previo["tipo"] == "nota_despacho" else ""))
        try:
            cur = con.execute(
                "INSERT INTO erp_documentos_venta (pedido_ref, tipo, numero, control, fecha, motivo, registrado_por) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ref, tipo, numero, control, _fecha(fecha or date.today().isoformat()), motivo, usuario_id))
        except Exception as e:
            if "uq_dv_control" in str(e) or "UNIQUE" in str(e):
                raise ValueError("ese número de control ya está registrado en otro documento")
            raise
        return cur.lastrowid


def convertir_en_factura(doc_id: int, numero, control, fecha, usuario_id: int):
    asegurar_tablas()
    numero, control = (numero or "").strip(), (control or "").strip()
    if not (numero and control):
        raise ValueError("la factura necesita su número y su número de control")
    with db.conectar() as con:
        d = con.execute("SELECT * FROM erp_documentos_venta WHERE id = ?", (doc_id,)).fetchone()
        if not d:
            raise ValueError("documento no encontrado")
        if d["tipo"] != "nota_despacho":
            raise ValueError("solo una nota de despacho se convierte en factura")
        try:
            con.execute("UPDATE erp_documentos_venta SET tipo = 'factura', numero = ?, control = ?, fecha = ?, registrado_por = ? WHERE id = ?",
                        (numero, control, _fecha(fecha or date.today().isoformat()), usuario_id, doc_id))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ValueError("ese número de control ya está registrado en otro documento")
            raise


def ventas(desde=None, hasta=None) -> dict:
    asegurar_tablas()
    d, h = _rango(desde, hasta)
    with db.conectar() as con:
        filas = con.execute(
            "SELECT p.id, p.orden, p.contacto_id, p.total, p.tasa_emision, p.estado_pago, p.estado, "
            f"date(COALESCE(p.pago_confirmado_en, p.fecha), '{LOCAL}') AS f, c.nombre, c.telefono "
            "FROM pedidos p JOIN contactos c ON c.id = p.contacto_id "
            "WHERE p.estado != 'cancelado' AND p.estado_pago IN ('pago_confirmado', 'a_credito') ORDER BY p.id").fetchall()
        docs = {x["pedido_ref"]: dict(x) for x in con.execute("SELECT * FROM erp_documentos_venta").fetchall()}
        adj = {x["entidad_id"]: x["n"] for x in con.execute("SELECT entidad_id, COUNT(*) AS n FROM erp_adjuntos WHERE entidad = 'venta' GROUP BY entidad_id").fetchall()}
    ordenes = {}
    for f in filas:
        g = ordenes.setdefault(f["orden"] or f"p{f['id']}", {"ref": f["id"], "cliente": f["nombre"] or f["telefono"], "fecha": f["f"], "total_usd": 0.0,
                                                            "tasa": f["tasa_emision"], "estado_pago": f["estado_pago"], "despachado": f["estado"] == "entregado"})
        g["total_usd"] = round(g["total_usd"] + (f["total"] or 0), 2)
    filas_out, resumen = [], {"ordenes": 0, "total_usd": 0.0, "con_factura_usd": 0.0, "despacho_pendiente_usd": 0.0, "nota_entrega_usd": 0.0, "sin_documento_usd": 0.0}
    for g in ordenes.values():
        if not (d <= g["fecha"] <= h):
            continue
        doc = docs.get(g["ref"])
        tipo = doc["tipo"] if doc else None
        soporte = "con" if tipo == "factura" else ("pendiente" if tipo == "nota_despacho" else "sin")
        g.update(documento=tipo, numero=doc["numero"] if doc else None, control=doc["control"] if doc else None, doc_id=doc["id"] if doc else None,
                 motivo=doc["motivo"] if doc else None, soporte=soporte, adjuntos=adj.get(g["ref"], 0),
                 total_bs=round(g["total_usd"] * g["tasa"], 2) if g["tasa"] else None)
        filas_out.append(g)
        resumen["ordenes"] += 1
        resumen["total_usd"] += g["total_usd"]
        clave = {"factura": "con_factura_usd", "nota_despacho": "despacho_pendiente_usd", "nota_entrega": "nota_entrega_usd"}.get(tipo, "sin_documento_usd")
        resumen[clave] += g["total_usd"]
    resumen = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in resumen.items()}
    limite = db._num_config("nota_entrega_alerta_pct", 10)
    resumen["nota_entrega_pct"] = round(resumen["nota_entrega_usd"] / resumen["total_usd"] * 100, 1) if resumen["total_usd"] else 0.0
    resumen["nota_entrega_limite_pct"] = limite
    resumen["alerta_nota_entrega"] = resumen["nota_entrega_pct"] > limite
    return {"filas": sorted(filas_out, key=lambda x: x["fecha"], reverse=True), "resumen": resumen}


# ---------------------------------------------------------------- compras y gastos
def registrar_compra(d: dict, usuario_id: int) -> int:
    asegurar_tablas()
    proveedor = (d.get("proveedor") or "").strip()
    if not proveedor:
        raise ValueError("falta el proveedor")
    tipo = d.get("tipo") if d.get("tipo") in ("mercancia", "servicio", "gasto") else "gasto"
    doc_tipo = d.get("doc_tipo") if d.get("doc_tipo") in ("factura", "ticket", "recibo", "otro") else "factura"
    moneda = (d.get("moneda") or "VES").upper()
    if moneda not in ("USD", "VES"):
        raise ValueError("moneda inválida")
    numero, control = (d.get("numero") or "").strip() or None, (d.get("control") or "").strip() or None
    if doc_tipo == "factura" and not (numero and control):
        raise ValueError("una factura de compra necesita su número y su número de control; si es un ticket o un recibo, elígelo como tal")
    monto = _num(d.get("monto"), "el monto", True)
    forma = d.get("forma_pago") if d.get("forma_pago") in ("banco", "caja_chica", "pendiente") else "pendiente"
    usd, bs, tasa = _montos(moneda, monto, _num(d.get("tasa"), "la tasa"))
    with db.conectar() as con:
        cur = con.execute(
            "INSERT INTO erp_compras (fecha, proveedor, rif, tipo, doc_tipo, numero, control, moneda, monto, base, iva, ret_iva, ret_islr, tasa, monto_usd, monto_bs, forma_pago, nota, registrado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_fecha(d.get("fecha") or date.today().isoformat()), proveedor, (d.get("rif") or "").strip() or None, tipo, doc_tipo, numero, control, moneda, monto,
             _num(d.get("base"), "la base"), _num(d.get("iva"), "el IVA"), _num(d.get("ret_iva"), "la retención de IVA"), _num(d.get("ret_islr"), "la retención de ISLR"),
             tasa, usd, bs, forma, (d.get("nota") or "").strip() or None, usuario_id))
        return cur.lastrowid


def compras(desde=None, hasta=None) -> dict:
    asegurar_tablas()
    d, h = _rango(desde, hasta)
    with db.conectar() as con:
        filas = [dict(x) for x in con.execute("SELECT * FROM erp_compras WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC, id DESC", (d, h)).fetchall()]
        adj = {x["entidad_id"]: x["n"] for x in con.execute("SELECT entidad_id, COUNT(*) AS n FROM erp_adjuntos WHERE entidad = 'compra' GROUP BY entidad_id").fetchall()}
    r = {"registros": len(filas), "con_soporte": 0, "sin_soporte": 0, "total_usd": 0.0, "total_bs": 0.0, "iva": 0.0, "ret_iva": 0.0, "ret_islr": 0.0}
    for x in filas:
        x["soporte"] = "con" if (x["doc_tipo"] == "factura" and x["numero"] and x["control"]) else "sin"
        x["adjuntos"] = adj.get(x["id"], 0)
        r["con_soporte" if x["soporte"] == "con" else "sin_soporte"] += 1
        r["total_usd"] += x["monto_usd"] or 0; r["total_bs"] += x["monto_bs"] or 0
        r["iva"] += x["iva"] or 0; r["ret_iva"] += x["ret_iva"] or 0; r["ret_islr"] += x["ret_islr"] or 0
    return {"filas": filas, "resumen": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}}


# ---------------------------------------------------------------- bancos y caja chica
def registrar_banco(d: dict, usuario_id: int) -> int:
    asegurar_tablas()
    cuenta, concepto = (d.get("cuenta") or "").strip(), (d.get("concepto") or "").strip()
    if not cuenta or not concepto:
        raise ValueError("faltan la cuenta y el concepto")
    tipo = d.get("tipo")
    if tipo not in ("ingreso", "egreso"):
        raise ValueError("el movimiento es un ingreso o un egreso")
    moneda = (d.get("moneda") or "VES").upper()
    if moneda not in ("USD", "VES"):
        raise ValueError("moneda inválida")
    monto = _num(d.get("monto"), "el monto", True)
    usd, bs, tasa = _montos(moneda, monto, _num(d.get("tasa"), "la tasa"))
    cid = int(d["compra_id"]) if str(d.get("compra_id") or "").isdigit() else None
    with db.conectar() as con:
        cur = con.execute(
            "INSERT INTO erp_bancos (fecha, cuenta, tipo, concepto, moneda, monto, tasa, monto_usd, monto_bs, referencia, compra_id, registrado_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_fecha(d.get("fecha") or date.today().isoformat()), cuenta, tipo, concepto, moneda, monto, tasa, usd, bs, (d.get("referencia") or "").strip() or None, cid, usuario_id))
        return cur.lastrowid


def bancos(desde=None, hasta=None) -> dict:
    asegurar_tablas()
    d, h = _rango(desde, hasta)
    with db.conectar() as con:
        filas = [dict(x) for x in con.execute("SELECT * FROM erp_bancos WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC, id DESC", (d, h)).fetchall()]
        adj = {x["entidad_id"]: x["n"] for x in con.execute("SELECT entidad_id, COUNT(*) AS n FROM erp_adjuntos WHERE entidad = 'banco' GROUP BY entidad_id").fetchall()}
    por_cuenta = {}
    for x in filas:
        x["adjuntos"] = adj.get(x["id"], 0)
        c = por_cuenta.setdefault(x["cuenta"], {"ingresos_bs": 0.0, "egresos_bs": 0.0, "ingresos_usd": 0.0, "egresos_usd": 0.0})
        k = "ingresos" if x["tipo"] == "ingreso" else "egresos"
        c[k + "_bs"] += x["monto_bs"] or 0
        c[k + "_usd"] += x["monto_usd"] or 0
    return {"filas": filas, "resumen": {"movimientos": len(filas), "por_cuenta": {k: {a: round(b, 2) for a, b in v.items()} for k, v in por_cuenta.items()}}}


# ---------------------------------------------------------------- inventario
def inventario(desde=None, hasta=None) -> dict:
    asegurar_tablas()
    d, h = _rango(desde, hasta)
    with db.conectar() as con:
        movs = [dict(x) for x in con.execute(
            f"SELECT m.id, date(m.fecha, '{LOCAL}') AS f, c.codigo, c.nombre, m.tipo, m.cantidad, m.stock_despues, m.costo_unitario, m.referencia "
            f"FROM erp_movimientos m JOIN catalogo c ON c.id = m.catalogo_id WHERE date(m.fecha, '{LOCAL}') BETWEEN ? AND ? ORDER BY m.id DESC", (d, h)).fetchall()]
        desp = con.execute(
            f"SELECT p.cantidad, COALESCE(dv.tipo, 'sin_documento') AS doc FROM pedidos p LEFT JOIN erp_documentos_venta dv ON dv.pedido_ref = "
            f"(SELECT MIN(id) FROM pedidos q WHERE q.orden = p.orden) WHERE p.estado = 'entregado' AND date(p.despachado_en, '{LOCAL}') BETWEEN ? AND ?", (d, h)).fetchall()
    por_doc = {}
    for x in desp:
        por_doc[x["doc"]] = por_doc.get(x["doc"], 0) + (x["cantidad"] or 0)
    total = sum(por_doc.values())
    sin = por_doc.get("nota_entrega", 0) + por_doc.get("sin_documento", 0) + por_doc.get("nota_despacho", 0)
    return {"filas": movs, "resumen": {"movimientos": len(movs), "unidades_despachadas": total, "despachadas_por_documento": por_doc,
                                       "despachadas_sin_factura": sin, "pct_sin_factura": round(sin / total * 100, 1) if total else 0.0}}


# ---------------------------------------------------------------- adjuntos
def _dir_adjuntos() -> str:
    p = os.path.join(os.path.dirname(os.path.abspath(db.RUTA_DB)), "adjuntos")
    os.makedirs(p, exist_ok=True)
    return p


def guardar_adjunto(entidad: str, entidad_id: int, nombre: str, datos: bytes, usuario_id: int) -> dict:
    asegurar_tablas()
    if entidad not in ENTIDADES:
        raise ValueError("entidad inválida")
    ext = os.path.splitext(nombre or "")[1].lower()
    if ext not in MIMES:
        raise ValueError("solo se aceptan PDF, PNG, JPG o WEBP")
    if len(datos) > MAX_ADJUNTO:
        raise ValueError("el archivo pesa más de 10 MB")
    if not datos.startswith({".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".webp": b"RIFF"}[ext]):
        raise ValueError("el contenido no corresponde al tipo de archivo")
    archivo = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_dir_adjuntos(), archivo), "wb") as f:
        f.write(datos)
    seguro = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(nombre))[:80]
    with db.conectar() as con:
        cur = con.execute("INSERT INTO erp_adjuntos (entidad, entidad_id, nombre, archivo, mime, bytes, subido_por) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (entidad, entidad_id, seguro, archivo, MIMES[ext], len(datos), usuario_id))
        return {"id": cur.lastrowid, "nombre": seguro}


def lista_adjuntos(entidad: str, entidad_id: int) -> list:
    asegurar_tablas()
    with db.conectar() as con:
        return [dict(x) for x in con.execute("SELECT id, nombre, mime, bytes, fecha FROM erp_adjuntos WHERE entidad = ? AND entidad_id = ? ORDER BY id", (entidad, entidad_id)).fetchall()]


def leer_adjunto(adj_id: int):
    asegurar_tablas()
    with db.conectar() as con:
        a = con.execute("SELECT * FROM erp_adjuntos WHERE id = ?", (adj_id,)).fetchone()
    if not a:
        return None
    ruta = os.path.join(_dir_adjuntos(), a["archivo"])
    return (dict(a), ruta) if os.path.exists(ruta) else None


# ---------------------------------------------------------------- CSV y carpeta mensual
def a_csv(columnas: list, filas: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([c[1] for c in columnas])
    for f in filas:
        w.writerow([("" if f.get(c[0]) is None else f.get(c[0])) for c in columnas])
    return "﻿" + buf.getvalue()


COLS = {
    "ventas": [("fecha", "fecha"), ("cliente", "cliente"), ("total_usd", "total_usd"), ("tasa", "tasa_cotizacion"), ("total_bs", "total_bs_aprox"), ("documento", "documento"),
               ("numero", "numero"), ("control", "control"), ("soporte", "soporte_fiscal"), ("motivo", "motivo_excepcion")],
    "compras": [("fecha", "fecha"), ("proveedor", "proveedor"), ("rif", "rif"), ("tipo", "tipo"), ("doc_tipo", "documento"), ("numero", "numero"), ("control", "control"),
                ("moneda", "moneda"), ("monto", "monto"), ("base", "base"), ("iva", "iva"), ("ret_iva", "retencion_iva"), ("ret_islr", "retencion_islr"), ("tasa", "tasa"),
                ("monto_usd", "monto_usd"), ("monto_bs", "monto_bs"), ("forma_pago", "forma_pago"), ("soporte", "soporte_fiscal"), ("nota", "nota")],
    "bancos": [("fecha", "fecha"), ("cuenta", "cuenta"), ("tipo", "tipo"), ("concepto", "concepto"), ("moneda", "moneda"), ("monto", "monto"), ("tasa", "tasa"),
               ("monto_usd", "monto_usd"), ("monto_bs", "monto_bs"), ("referencia", "referencia")],
    "inventario": [("f", "fecha"), ("codigo", "codigo"), ("nombre", "producto"), ("tipo", "movimiento"), ("cantidad", "cantidad"), ("stock_despues", "existencia_despues"),
                   ("costo_unitario", "costo_unitario"), ("referencia", "referencia")],
}


def reporte(nombre: str, desde=None, hasta=None) -> dict:
    return {"ventas": ventas, "compras": compras, "bancos": bancos, "inventario": inventario}[nombre](desde, hasta)


def carpeta_mensual(mes: str) -> bytes:
    """ZIP del mes (AAAA-MM): reportes en CSV, ingresos, diferencial cambiario y todos los soportes adjuntos."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes or ""):
        raise ValueError("el mes debe ser AAAA-MM")
    y, m = int(mes[:4]), int(mes[5:])
    ultimo = (date(y + (m == 12), (m % 12) + 1, 1) - date(y, m, 1)).days
    d, h = f"{mes}-01", f"{mes}-{ultimo:02d}"
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for n in ("ventas", "compras", "bancos", "inventario"):
            z.writestr(f"reportes/{n}.csv", a_csv(COLS[n], reporte(n, d, h)["filas"]))
        ing = db.relacion_ingresos(d, h)
        z.writestr("reportes/ingresos.csv", a_csv([("fecha", "fecha"), ("nombre", "cliente"), ("metodo", "metodo"), ("moneda", "moneda"), ("monto_original", "monto"), ("tasa_pago", "tasa"),
                                                   ("monto_usd", "monto_usd"), ("monto_bs", "monto_bs"), ("banco", "banco"), ("referencia", "referencia")], ing["pagos"]))
        z.writestr("reportes/diferencial_cambiario.csv", a_csv([("fecha", "fecha"), ("nombre", "cliente"), ("tipo", "nota"), ("base_usd", "base_usd"), ("tasa_emision", "tasa_cotizacion"),
                                                                ("tasa_pago", "tasa_pago"), ("monto_bs", "monto_bs"), ("estado", "estado")], ing["notas"]))
        ids = {"venta": {x["ref"] for x in ventas(d, h)["filas"]}, "compra": {x["id"] for x in compras(d, h)["filas"]},
               "banco": {x["id"] for x in bancos(d, h)["filas"]}, "pago": {x["id"] for x in ing["pagos"]}}
        n_adj = 0
        with db.conectar() as con:
            todos = con.execute("SELECT * FROM erp_adjuntos ORDER BY id").fetchall()
        for a in todos:
            if a["entidad_id"] in ids.get(a["entidad"], ()):
                ruta = os.path.join(_dir_adjuntos(), a["archivo"])
                if os.path.exists(ruta):
                    z.write(ruta, f"soportes/{a['entidad']}/{a['entidad_id']}-{a['nombre']}")
                    n_adj += 1
        z.writestr("LEEME.txt", (
            f"Carpeta de soportes {mes} · Grupo Vextor\n\n"
            "Reportes (CSV, separador punto y coma, abre en Excel): ventas, compras, bancos y caja chica, inventario (movimientos), ingresos y diferencial cambiario.\n"
            "Columna 'soporte_fiscal': con = factura con número de control; sin = ticket, recibo o nota de entrega; pendiente = nota de despacho por facturar.\n"
            f"Soportes adjuntos incluidos: {n_adj} (carpeta soportes/).\n\n"
            "Estos reportes son insumos administrativos. No son libros contables ni documentos fiscales: la contadora decide qué se declara.\n"))
    return mem.getvalue()
