"""
ERP de Ventas-24, primera entrega: inventario, proveedores y compras.

Reglas que mantienen el orden (ver arquitectura):
- El stock solo cambia por movimientos: cada cambio deja una fila en erp_movimientos
  con quién, cuándo y por qué. `catalogo.stock` es el saldo actual de ese historial.
- El CRM y el ERP se hablan por eventos (tabla eventos_outbox), no se editan las tablas
  del otro. Aquí: al recibir una compra se emite `stock.repuesto`, y el CRM avisa a la
  lista de espera.
- Todo lo que mueve dinero o inventario queda en manos de una persona (admin): las
  órdenes de compra se aprueban y se reciben a mano.

Estados de una orden de compra:  borrador -> aprobada -> recibida   (o cancelada)
"""

import db

TIPOS_MOVIMIENTO = ("conteo", "recepcion", "reserva", "liberacion", "ajuste")


# ---------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------

def inventario(q: str | None = None, solo_bajo: bool = False, limite: int = 200):
    """Productos con su existencia. `solo_bajo` = los que están en o bajo el mínimo (y tienen mínimo definido)."""
    with db.conectar() as con:
        filas = con.execute("SELECT * FROM catalogo").fetchall()
    tokens = db.palabras_clave(q) if q else []
    salida = []
    for p in filas:
        if solo_bajo and not (p["stock_minimo"] > 0 and p["stock_verificado"] and p["stock"] <= p["stock_minimo"]):
            continue
        if tokens:
            pajar = db._normalizar(f'{p["nombre"]} {p["codigo"] or ""} {p["marca"]}')
            if not all(db._coincide(t, pajar) for t in tokens):
                continue
        salida.append({
            "id": p["id"], "codigo": p["codigo"], "nombre": p["nombre"], "marca": p["marca"],
            "stock": p["stock"], "verificado": bool(p["stock_verificado"]), "minimo": p["stock_minimo"],
            "costo": p["costo"], "precio": p["precio"], "precio_mayor": p["precio_mayor"],
            "bajo": bool(p["stock_minimo"] > 0 and p["stock_verificado"] and p["stock"] <= p["stock_minimo"]),
        })
    salida.sort(key=lambda x: (not x["bajo"], x["nombre"]))
    return salida[:limite], len(salida)


def resumen_inventario():
    with db.conectar() as con:
        r = con.execute(
            "SELECT COUNT(*) AS productos, "
            "SUM(CASE WHEN stock_verificado = 1 THEN 1 ELSE 0 END) AS con_inventario, "
            "SUM(CASE WHEN stock_minimo > 0 AND stock_verificado = 1 AND stock <= stock_minimo THEN 1 ELSE 0 END) AS bajos, "
            "SUM(CASE WHEN stock_verificado = 1 AND costo IS NOT NULL THEN stock * costo ELSE 0 END) AS valor_costo "
            "FROM catalogo"
        ).fetchone()
        oc = con.execute(
            "SELECT SUM(CASE WHEN estado = 'borrador' THEN 1 ELSE 0 END) AS borrador, "
            "SUM(CASE WHEN estado = 'aprobada' THEN 1 ELSE 0 END) AS por_recibir FROM erp_ordenes_compra"
        ).fetchone()
    return {
        "productos": r["productos"], "con_inventario": r["con_inventario"] or 0, "bajos": r["bajos"] or 0,
        "valor_a_costo": round(r["valor_costo"] or 0, 2),
        "compras_borrador": oc["borrador"] or 0, "compras_por_recibir": oc["por_recibir"] or 0,
    }


def _producto(con, codigo=None, catalogo_id=None, marca=None):
    if catalogo_id:
        return con.execute("SELECT * FROM catalogo WHERE id = ?", (catalogo_id,)).fetchone()
    if marca:
        return con.execute("SELECT * FROM catalogo WHERE lower(codigo) = lower(?) AND lower(marca) = lower(?)",
                           (codigo, marca)).fetchone()
    filas = con.execute("SELECT * FROM catalogo WHERE lower(codigo) = lower(?)", (codigo,)).fetchall()
    return filas[0] if len(filas) == 1 else None   # código repetido en varias marcas: hay que indicar la marca


def contar(catalogo_id: int, cantidad: int, usuario_id: int, nota: str | None = None, minimo: int | None = None,
           costo: float | None = None):
    """Conteo físico: fija la existencia real de un producto. Deja un movimiento por la diferencia."""
    if cantidad < 0:
        raise ValueError("la cantidad no puede ser negativa")
    with db.conectar() as con:
        p = con.execute("SELECT * FROM catalogo WHERE id = ?", (catalogo_id,)).fetchone()
        if not p:
            raise ValueError("producto no encontrado")
        delta = cantidad - (p["stock"] if p["stock_verificado"] else 0)
        con.execute(
            "UPDATE catalogo SET stock = ?, stock_verificado = 1, stock_prueba = 0, "
            "stock_minimo = COALESCE(?, stock_minimo), costo = COALESCE(?, costo) WHERE id = ?",
            (cantidad, minimo, costo, catalogo_id),
        )
        db.registrar_movimiento(con, catalogo_id, "conteo", delta, nota=nota or "conteo físico",
                                usuario_id=usuario_id, costo=costo)
    return {"id": catalogo_id, "stock": cantidad, "diferencia": delta}


def cargar_conteo(filas: list, usuario_id: int):
    """Carga masiva de existencias reales. filas: dicts con codigo, cantidad y opcionales marca, costo, minimo."""
    hechas, no_encontradas, ambiguas, invalidas = 0, [], [], 0
    for f in filas:
        try:
            cantidad = int(float(str(f.get("cantidad", "")).replace(",", ".")))
            costo = float(str(f["costo"]).replace(",", ".")) if str(f.get("costo", "")).strip() else None
            minimo = int(float(str(f["minimo"]).replace(",", "."))) if str(f.get("minimo", "")).strip() else None
        except (ValueError, TypeError):
            invalidas += 1
            continue
        with db.conectar() as con:
            if f.get("marca"):
                p = _producto(con, codigo=f.get("codigo"), marca=f["marca"])
            else:
                n = con.execute("SELECT COUNT(*) AS n FROM catalogo WHERE lower(codigo) = lower(?)",
                                (f.get("codigo", ""),)).fetchone()["n"]
                if n > 1:
                    ambiguas.append(f.get("codigo"))
                    continue
                p = _producto(con, codigo=f.get("codigo"))
        if not p:
            no_encontradas.append(f.get("codigo"))
            continue
        contar(p["id"], max(cantidad, 0), usuario_id, "carga de inventario", minimo, costo)
        hechas += 1
    return {"actualizados": hechas, "no_encontrados": no_encontradas[:50], "codigos_ambiguos": ambiguas[:50],
            "filas_invalidas": invalidas}


def movimientos(catalogo_id: int | None = None, limite: int = 100):
    donde, params = "", []
    if catalogo_id:
        donde, params = " WHERE m.catalogo_id = ?", [catalogo_id]
    with db.conectar() as con:
        filas = con.execute(
            "SELECT m.*, c.codigo, c.nombre, u.nombre AS usuario FROM erp_movimientos m "
            "JOIN catalogo c ON c.id = m.catalogo_id LEFT JOIN usuarios u ON u.id = m.usuario_id "
            f"{donde} ORDER BY m.id DESC LIMIT ?",
            (*params, limite),
        ).fetchall()
    return [dict(f) for f in filas]


# ---------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------

def crear_proveedor(nombre: str, contacto=None, telefono=None, notas=None) -> int:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("el proveedor necesita un nombre")
    with db.conectar() as con:
        try:
            cur = con.execute(
                "INSERT INTO erp_proveedores (nombre, contacto, telefono, notas) VALUES (?, ?, ?, ?)",
                (nombre, contacto, telefono, notas),
            )
        except Exception:
            raise ValueError("ya existe un proveedor con ese nombre")
        return cur.lastrowid


def listar_proveedores():
    with db.conectar() as con:
        filas = con.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM erp_ordenes_compra o WHERE o.proveedor_id = p.id) AS ordenes "
            "FROM erp_proveedores p WHERE p.activo = 1 ORDER BY p.nombre"
        ).fetchall()
    return [dict(f) for f in filas]


# ---------------------------------------------------------------------
# Compras
# ---------------------------------------------------------------------

def crear_orden_compra(proveedor_id: int, lineas: list, nota: str | None, usuario_id: int) -> int:
    """lineas: dicts con (catalogo_id o codigo[+marca]), cantidad y opcional costo."""
    if not lineas:
        raise ValueError("la orden necesita al menos una línea")
    with db.conectar() as con:
        if not con.execute("SELECT 1 FROM erp_proveedores WHERE id = ? AND activo = 1", (proveedor_id,)).fetchone():
            raise ValueError("proveedor no encontrado")
        validas = []
        for i, l in enumerate(lineas, 1):
            p = _producto(con, codigo=l.get("codigo"), catalogo_id=l.get("catalogo_id"), marca=l.get("marca"))
            if not p:
                raise ValueError(f"línea {i}: producto no encontrado o código repetido (indica la marca)")
            try:
                cantidad = int(l["cantidad"])
            except (KeyError, ValueError, TypeError):
                raise ValueError(f"línea {i}: cantidad inválida")
            if cantidad < 1:
                raise ValueError(f"línea {i}: la cantidad debe ser al menos 1")
            costo = l.get("costo")
            costo = float(str(costo).replace(",", ".")) if costo not in (None, "") else p["costo"]
            validas.append((p["id"], cantidad, costo))
        cur = con.execute(
            "INSERT INTO erp_ordenes_compra (proveedor_id, nota, creada_por) VALUES (?, ?, ?)",
            (proveedor_id, nota, usuario_id),
        )
        orden_id = cur.lastrowid
        con.executemany(
            "INSERT INTO erp_oc_lineas (orden_id, catalogo_id, cantidad, costo_unitario) VALUES (?, ?, ?, ?)",
            [(orden_id, cid, cant, costo) for cid, cant, costo in validas],
        )
    return orden_id


def listar_ordenes(estado: str | None = None):
    donde, params = "", []
    if estado:
        donde, params = " WHERE o.estado = ?", [estado]
    with db.conectar() as con:
        ordenes = con.execute(
            "SELECT o.*, p.nombre AS proveedor FROM erp_ordenes_compra o "
            f"JOIN erp_proveedores p ON p.id = o.proveedor_id{donde} ORDER BY o.id DESC",
            params,
        ).fetchall()
        resultado = []
        for o in ordenes:
            lineas = con.execute(
                "SELECT l.*, c.codigo, c.nombre, c.marca FROM erp_oc_lineas l JOIN catalogo c ON c.id = l.catalogo_id "
                "WHERE l.orden_id = ? ORDER BY l.id",
                (o["id"],),
            ).fetchall()
            d = dict(o)
            d["lineas"] = [dict(l) for l in lineas]
            d["total"] = round(sum((l["costo_unitario"] or 0) * l["cantidad"] for l in lineas), 2)
            resultado.append(d)
    return resultado


def _cambiar_estado(orden_id: int, desde: str, hacia: str, usuario_id: int, columnas: str):
    with db.conectar() as con:
        o = con.execute("SELECT estado FROM erp_ordenes_compra WHERE id = ?", (orden_id,)).fetchone()
        if not o:
            raise ValueError("orden no encontrada")
        if o["estado"] != desde:
            raise ValueError(f"la orden está '{o['estado']}', no '{desde}'")
        con.execute(
            f"UPDATE erp_ordenes_compra SET estado = ?, {columnas} = ?, "
            f"{columnas.replace('_por', '_en')} = CURRENT_TIMESTAMP WHERE id = ?",
            (hacia, usuario_id, orden_id),
        )


def aprobar_orden(orden_id: int, usuario_id: int):
    _cambiar_estado(orden_id, "borrador", "aprobada", usuario_id, "aprobada_por")


def cancelar_orden(orden_id: int):
    with db.conectar() as con:
        cur = con.execute(
            "UPDATE erp_ordenes_compra SET estado = 'cancelada' WHERE id = ? AND estado IN ('borrador', 'aprobada')",
            (orden_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("solo se cancelan órdenes en borrador o aprobadas")


def recibir_orden(orden_id: int, usuario_id: int, recibidos: dict | None = None):
    """Mercancía recibida: suma al stock con un movimiento por línea, actualiza el costo y emite
    `stock.repuesto` por cada producto que estaba agotado. `recibidos` = {linea_id: cantidad};
    sin él se recibe todo lo pedido. Devuelve los productos repuestos."""
    repuestos = []
    with db.conectar() as con:
        o = con.execute("SELECT estado FROM erp_ordenes_compra WHERE id = ?", (orden_id,)).fetchone()
        if not o:
            raise ValueError("orden no encontrada")
        if o["estado"] != "aprobada":
            raise ValueError(f"la orden está '{o['estado']}': solo se reciben órdenes aprobadas")
        lineas = con.execute("SELECT * FROM erp_oc_lineas WHERE orden_id = ?", (orden_id,)).fetchall()
        for l in lineas:
            cant = int((recibidos or {}).get(str(l["id"]), (recibidos or {}).get(l["id"], l["cantidad"])))
            if cant < 0:
                raise ValueError("no se puede recibir una cantidad negativa")
            if cant == 0:
                continue
            p = con.execute("SELECT * FROM catalogo WHERE id = ?", (l["catalogo_id"],)).fetchone()
            antes = p["stock"] if p["stock_verificado"] else 0
            con.execute(
                "UPDATE catalogo SET stock = ?, stock_verificado = 1, stock_prueba = 0, "
                "costo = COALESCE(?, costo) WHERE id = ?",
                (antes + cant, l["costo_unitario"], l["catalogo_id"]),
            )
            con.execute("UPDATE erp_oc_lineas SET recibido = ? WHERE id = ?", (cant, l["id"]))
            db.registrar_movimiento(con, l["catalogo_id"], "recepcion", cant, referencia=f"compra #{orden_id}",
                                    usuario_id=usuario_id, costo=l["costo_unitario"])
            if antes <= 0:
                repuestos.append({"catalogo_id": l["catalogo_id"], "codigo": p["codigo"], "nombre": p["nombre"]})
        con.execute(
            "UPDATE erp_ordenes_compra SET estado = 'recibida', recibida_por = ?, recibida_en = CURRENT_TIMESTAMP WHERE id = ?",
            (usuario_id, orden_id),
        )
        for r in repuestos:
            db.emitir_evento(con, "stock.repuesto", r)
    return repuestos


def sugerir_reposicion():
    """Productos con inventario real en o bajo su mínimo, con la cantidad que falta para duplicar el mínimo."""
    productos, _ = inventario(solo_bajo=True, limite=500)
    return [dict(p, sugerido=max(p["minimo"] * 2 - p["stock"], 1)) for p in productos]
