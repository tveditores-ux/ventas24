"""
Remoto para los vendedores humanos (página /vendedor, pensada para el teléfono).

Lo que puede hacer un vendedor:
  - ver la disponibilidad y los precios (nunca el costo ni el margen),
  - ver SUS clientes (los que tiene asignados), tomar los que aún no tienen dueño, y crear clientes nuevos,
  - armar un pedido o una cotización, con el mismo mínimo al mayor y la misma reserva de inventario del bot,
  - cargar el comprobante de pago (pasa por el agente de seguridad) y pedirle condiciones al equipo.

Todo precio sale de la base: el teléfono nunca decide un monto. Un vendedor solo toca lo suyo; admin y super_admin ven todo.
"""

import re
import uuid
from datetime import datetime

import db


def es_vendedor(u) -> bool:
    return u["rol"] == "vendedor"


def _contacto(u, cid: int) -> dict:
    with db.conectar() as con:
        c = con.execute("SELECT * FROM contactos WHERE id = ?", (cid,)).fetchone()
    if not c:
        raise ValueError("cliente no encontrado")
    if es_vendedor(u) and c["asignado_a"] != u["id"]:
        raise ValueError("ese cliente no está asignado a ti")
    return dict(c)


def normalizar_telefono_ve(t: str) -> str:
    d = re.sub(r"[^\d+]", "", t or "")
    if d.startswith("+"):
        num = "+" + re.sub(r"\D", "", d)
    else:
        dig = re.sub(r"\D", "", d)
        if dig.startswith("58") and len(dig) >= 12:
            num = "+" + dig
        elif dig.startswith("0") and len(dig) == 11:
            num = "+58" + dig[1:]
        elif len(dig) == 10 and dig.startswith("4"):
            num = "+58" + dig
        else:
            num = "+" + dig
    if not re.fullmatch(r"\+\d{10,15}", num):
        raise ValueError("el teléfono no es válido (ejemplo: 0412 123 4567)")
    return num


# ---------------------------------------------------------------- inicio
def resumen(u) -> dict:
    filtro = "AND c.asignado_a = ?" if es_vendedor(u) else ""
    arg = [u["id"]] if es_vendedor(u) else []
    mes = datetime.now().strftime("%Y-%m")
    with db.conectar() as con:
        def uno(sql, *a):
            return con.execute(sql, [*a, *arg]).fetchone()[0]
        por_pagar = uno(f"SELECT COUNT(DISTINCT COALESCE(p.orden, p.id)) FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' AND p.estado_pago = 'por_pagar' {filtro}")
        por_confirmar = uno(f"SELECT COUNT(DISTINCT COALESCE(p.orden, p.id)) FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' AND p.estado_pago = 'por_confirmar' {filtro}")
        en_revision = uno(f"SELECT COUNT(DISTINCT COALESCE(p.orden, p.id)) FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' AND p.estado_pago = 'comprobante_recibido' {filtro}")
        credito_vencido = uno(f"SELECT COUNT(DISTINCT COALESCE(p.orden, p.id)) FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' AND p.estado_pago = 'a_credito' AND p.credito_hasta < date('now') {filtro}")
        mis_clientes = con.execute("SELECT COUNT(*) FROM contactos c WHERE 1=1 " + filtro, arg).fetchone()[0]
        ventas = con.execute(
            "SELECT COALESCE(SUM(p.total), 0) FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' "
            f"AND p.estado_pago IN ('pago_confirmado', 'a_credito') AND substr(date(COALESCE(p.pago_confirmado_en, p.fecha), '-4 hours'), 1, 7) = ? {filtro}", [mes, *arg]).fetchone()[0]
        libres = con.execute("SELECT COUNT(*) FROM contactos WHERE asignado_a IS NULL AND (wecall_ultimo_id IS NOT NULL OR EXISTS (SELECT 1 FROM interacciones WHERE contacto_id = contactos.id))").fetchone()[0] if es_vendedor(u) else 0
    return {"por_pagar": por_pagar, "por_confirmar": por_confirmar, "en_revision": en_revision, "credito_vencido": credito_vencido,
            "clientes": mis_clientes, "por_tomar": libres, "ventas_mes_usd": round(ventas, 2), "mes": mes, "tasa": db.tasa_vigente()}


# ---------------------------------------------------------------- disponibilidad (sin costos)
def disponibilidad(q: str, tipo_cliente: str | None = None, limite: int = 20) -> list:
    tokens = [t for t in re.split(r"\s+", (q or "").strip().lower()) if t][:6]
    if not tokens:
        return []
    cond = " AND ".join(["(lower(COALESCE(codigo,'')) LIKE ? OR lower(nombre) LIKE ? OR lower(marca) LIKE ? OR lower(modelo) LIKE ? OR codigo_barras = ?)"] * len(tokens))
    args = []
    for t in tokens:
        like = f"%{t}%"
        args += [like, like, like, like, t]
    with db.conectar() as con:
        filas = con.execute(
            "SELECT id, codigo, nombre, marca, modelo, stock, stock_verificado, stock_minimo, precio, precio_mayor FROM catalogo "
            f"WHERE {cond} ORDER BY (lower(codigo) = ?) DESC, (stock_verificado = 0), nombre LIMIT ?", [*args, tokens[0], limite]).fetchall()
    salida = []
    for f in filas:
        d = dict(f)
        d["precio_cliente"] = db.precio_para(f, "mayorista" if tipo_cliente == "mayorista" else None)
        d["estado_stock"] = "por_confirmar" if not f["stock_verificado"] else ("agotado" if f["stock"] <= 0 else ("poco" if f["stock_minimo"] and f["stock"] <= f["stock_minimo"] else "disponible"))
        salida.append(d)
    return salida


# ---------------------------------------------------------------- clientes
def clientes(u, q: str = "", modo: str = "mios") -> list:
    q = (q or "").strip().lower()
    cond, args = [], []
    if modo == "libres":
        if not es_vendedor(u):
            return []
        cond.append("c.asignado_a IS NULL AND (c.wecall_ultimo_id IS NOT NULL OR EXISTS (SELECT 1 FROM interacciones i WHERE i.contacto_id = c.id))")
    elif es_vendedor(u):
        cond.append("c.asignado_a = ?"); args.append(u["id"])
    if q:
        cond.append("(lower(COALESCE(c.nombre,'')) LIKE ? OR c.telefono LIKE ? OR lower(COALESCE(c.negocio,'')) LIKE ?)"); args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    donde = ("WHERE " + " AND ".join(cond)) if cond else ""
    with db.conectar() as con:
        filas = con.execute(
            "SELECT c.id, c.nombre, c.negocio, c.telefono, c.tipo, c.estado, c.ciudad, c.fecha_ultimo_contacto, c.limite_credito, u.nombre AS vendedor, "
            "(SELECT COUNT(DISTINCT COALESCE(p.orden, p.id)) FROM pedidos p WHERE p.contacto_id = c.id AND p.estado != 'cancelado' AND p.estado_pago IN ('por_confirmar','por_pagar','comprobante_recibido')) AS abiertos "
            f"FROM contactos c LEFT JOIN usuarios u ON u.id = c.asignado_a {donde} ORDER BY c.fecha_ultimo_contacto DESC, c.id DESC LIMIT 100", args).fetchall()
    out = []
    for f in filas:
        d = dict(f); d["saldo"] = db.saldo_credito(f["id"]); out.append(d)
    return out


def cliente_detalle(u, cid: int) -> dict:
    c = _contacto(u, cid)
    with db.conectar() as con:
        ped = con.execute(
            "SELECT MIN(id) AS id, orden, MAX(fecha) AS fecha, ROUND(SUM(total), 2) AS total, MAX(estado_pago) AS estado_pago, MAX(estado) AS estado, GROUP_CONCAT(cantidad || ' x ' || substr(producto, 1, 40), '; ') AS lineas "
            "FROM pedidos WHERE contacto_id = ? AND estado != 'cancelado' GROUP BY COALESCE(orden, id) ORDER BY MAX(id) DESC LIMIT 15", (cid,)).fetchall()
    c["pedidos"] = [dict(p) for p in ped]
    c["saldo"] = db.saldo_credito(cid)
    return {k: c[k] for k in ("id", "nombre", "negocio", "telefono", "tipo", "estado", "ciudad", "notas", "limite_credito", "saldo", "pedidos", "fecha_ultimo_contacto")}


def crear_cliente(u, nombre: str, telefono: str, tipo: str, negocio: str = "", ciudad: str = "", notas: str = "") -> int:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("falta el nombre")
    tel = normalizar_telefono_ve(telefono)
    tipo = "mayorista" if tipo == "mayorista" else "cliente"
    with db.conectar() as con:
        ya = con.execute("SELECT id, asignado_a FROM contactos WHERE telefono = ?", (tel,)).fetchone()
    if ya:
        if es_vendedor(u) and ya["asignado_a"] != u["id"]:
            raise ValueError("ese teléfono ya es de otro cliente; si no tiene dueño, tómalo desde «Por tomar»" if ya["asignado_a"] is None else "ese cliente ya está asignado a otro vendedor")
        raise ValueError("ya tienes un cliente con ese teléfono")
    cid = db.obtener_o_crear_contacto(tel, tipo=tipo)
    db.actualizar_contacto(cid, nombre=nombre, negocio=(negocio or "").strip() or None, ciudad=(ciudad or "").strip() or None,
                           notas=(f"[{datetime.now():%d/%m/%Y}] {u['nombre']}: {notas.strip()}" if (notas or "").strip() else ""), asignado_a=u["id"], estado="nuevo")
    db.registrar_accion("Vendedor", cid, "crear_cliente", {"telefono": tel, "tipo": tipo}, u["nombre"])
    return cid


def tomar_cliente(u, cid: int) -> bool:
    if not es_vendedor(u):
        raise ValueError("solo los vendedores toman clientes sin dueño")
    ok = db.asignar_conversacion_si_libre(cid, u["id"])
    if not ok:
        raise ValueError("ese cliente ya es de otro vendedor")
    db.registrar_accion("Vendedor", cid, "tomar_cliente", {}, u["nombre"])
    return True


def nota_cliente(u, cid: int, texto: str):
    texto = (texto or "").strip()
    if not texto or len(texto) > 600:
        raise ValueError("escribe la nota (hasta 600 caracteres)")
    c = _contacto(u, cid)
    nueva = ((c.get("notas") or "") + f"\n[{datetime.now():%d/%m/%Y}] {u['nombre']}: {texto}").strip()
    db.actualizar_contacto(cid, notas=nueva[-6000:])


# ---------------------------------------------------------------- pedidos
def pedidos(u) -> list:
    filtro = "AND c.asignado_a = ?" if es_vendedor(u) else ""
    arg = [u["id"]] if es_vendedor(u) else []
    with db.conectar() as con:
        filas = con.execute(
            "SELECT MIN(p.id) AS id, p.orden, c.id AS contacto_id, COALESCE(c.nombre, c.telefono) AS cliente, MAX(p.fecha) AS fecha, ROUND(SUM(p.total), 2) AS total, "
            "MAX(p.estado_pago) AS estado_pago, MAX(p.estado) AS estado, MIN(p.reservado_hasta) AS reservado_hasta, MAX(p.credito_hasta) AS credito_hasta, "
            "GROUP_CONCAT(p.cantidad || ' x ' || substr(p.producto, 1, 38), '; ') AS lineas "
            f"FROM pedidos p JOIN contactos c ON c.id = p.contacto_id WHERE p.estado != 'cancelado' {filtro} GROUP BY COALESCE(p.orden, p.id) ORDER BY MAX(p.id) DESC LIMIT 60", arg).fetchall()
    return [dict(f) for f in filas]


def crear_pedido(u, contacto_id: int, items: list, forzar_minimo: bool = False) -> dict:
    c = _contacto(u, contacto_id)
    if not isinstance(items, list) or not items:
        raise ValueError("agrega al menos un producto")
    if len(items) > 60:
        raise ValueError("un pedido admite hasta 60 productos")
    tipo = "mayorista" if c["tipo"] == "mayorista" else None
    lineas, vistos = [], set()
    for i, it in enumerate(items, 1):
        try:
            pid, cant = int(it["catalogo_id"]), int(it["cantidad"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"línea {i}: datos inválidos")
        if cant < 1 or cant > 100000:
            raise ValueError(f"línea {i}: la cantidad debe estar entre 1 y 100000")
        if pid in vistos:
            raise ValueError(f"línea {i}: el producto está repetido; suma las cantidades")
        vistos.add(pid)
        with db.conectar() as con:
            f = con.execute("SELECT * FROM catalogo WHERE id = ?", (pid,)).fetchone()
        if not f:
            raise ValueError(f"línea {i}: producto no encontrado")
        precio = db.precio_para(f, tipo)
        if not precio or precio <= 0:
            raise ValueError(f"{f['nombre'][:40]}: no tiene precio cargado")
        if f["stock_verificado"] and f["stock"] < cant:
            raise ValueError(f"{f['nombre'][:40]}: solo hay {f['stock']} en existencia")
        nombre = f"[{f['codigo']}] {f['nombre'][:80]} ({f['marca']})" if f["codigo"] else f"{f['nombre']} - {f['marca']} {f['modelo']}".strip()
        lineas.append({"fila": f, "producto": nombre, "cantidad": cant, "precio": precio, "catalogo_id": pid})
    total = round(sum(l["cantidad"] * l["precio"] for l in lineas), 2)
    unidades = sum(l["cantidad"] for l in lineas)
    if c["tipo"] == "mayorista" and not db.cumple_pedido_minimo(total, unidades) and not (forzar_minimo and not es_vendedor(u)):
        m = db.pedido_minimo()
        raise ValueError(f"el pedido al mayor es de ${m['usd']:.0f} o {m['unidades']} unidades (lleva ${total:.2f} y {unidades} unidades). Completa el pedido, o pide una excepción al equipo")
    auto = all(l["fila"]["stock_verificado"] for l in lineas)
    estado_pago = "por_pagar" if auto else "por_confirmar"
    orden = uuid.uuid4().hex[:10]
    db.crear_orden(contacto_id, orden, [{"producto": l["producto"], "cantidad": l["cantidad"], "precio": l["precio"], "catalogo_id": l["catalogo_id"], "reservar": auto} for l in lineas],
                   estado_pago, u["nombre"] if auto else None)
    with db.conectar() as con:
        primera = con.execute("SELECT id FROM pedidos WHERE orden = ? ORDER BY id LIMIT 1", (orden,)).fetchone()["id"]
    db.actualizar_contacto(contacto_id, estado="cliente")
    db.tocar_ultimo_contacto(contacto_id)
    db.registrar_accion("Vendedor", contacto_id, "pedido_vendedor", {"orden": orden, "lineas": len(lineas), "total": total}, u["nombre"])
    return {"pedido_id": primera, "orden": orden, "total": total, "unidades": unidades, "estado_pago": estado_pago, "reservado": auto,
            "mensaje": ("Existencia confirmada y reservada: el cliente ya puede pagar." if auto else "Hay productos sin existencia verificada: el equipo la confirma antes de cobrar.")}


def texto_cliente(u, pedido_id: int) -> tuple:
    """(texto, contacto) con el total y los datos de pago, para mandárselo al cliente."""
    import ciclo
    p = db.obtener_pedido(pedido_id)
    if not p:
        raise ValueError("pedido no encontrado")
    _contacto(u, p["contacto_id"])
    if p["estado_pago"] != "por_pagar":
        raise ValueError("este pedido no está esperando el pago")
    return ciclo.texto_existencia_confirmada(pedido_id), p


def avisar_pedido(u, pedido_id: int) -> dict:
    import ciclo
    texto, p = texto_cliente(u, pedido_id)
    ok, motivo = ciclo.notificar_cliente(p["contacto_id"], texto)
    db.registrar_accion("Vendedor", p["contacto_id"], "avisar_pedido", {"pedido": pedido_id, "enviado": ok}, u["nombre"])
    return {"enviado": ok, "motivo": None if ok else motivo, "texto": texto, "telefono": p["telefono"]}


def solicitud(u, contacto_id: int, tipo: str, asunto: str, detalle: str) -> int:
    c = _contacto(u, contacto_id)
    tipo = tipo if tipo in ("condiciones", "existencia", "pago", "otro") else "otro"
    asunto = (asunto or "").strip()
    if not asunto:
        raise ValueError("escribe qué necesitas")
    sid = db.crear_solicitud(contacto_id, tipo, f"{u['nombre']}: {asunto}"[:200], ((detalle or "").strip() + f"\n(Pedido por el vendedor {u['nombre']} para {c.get('nombre') or c['telefono']})").strip())
    db.registrar_accion("Vendedor", contacto_id, "solicitud_vendedor", {"tipo": tipo}, u["nombre"])
    return sid
