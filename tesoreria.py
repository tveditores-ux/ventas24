"""
Bancos y cajas: cuentas con su saldo, transferencias entre cuentas, conciliación con el estado de cuenta y pago de compras pendientes.

Una cuenta es un banco, una caja de efectivo o una billetera (Zelle, Pago Móvil, Binance…), en dólares o en bolívares. Su saldo es
el saldo inicial más lo que entró y salió desde esa fecha, y junta dos fuentes:
  - movimientos de erp_bancos cuyo campo «cuenta» es el nombre de la cuenta (cargas manuales, por lote y transferencias);
  - pagos de pedidos confirmados cuyo comprobante dice que se pagó a esa cuenta (nombre o alias).
Una transferencia entre cuentas son dos movimientos con el mismo transferencia_id; no es ingreso ni egreso del negocio y no entra
en el tablero de resultados. Conciliar es marcar qué movimientos ya aparecen en el estado de cuenta del banco.
No se guardan números de cuenta completos: solo los últimos 4 dígitos.
"""

import re
import uuid
from datetime import date, datetime, timedelta

import contable
import db
import finanzas

TIPOS = {"banco": "Cuenta bancaria", "efectivo": "Efectivo o caja", "billetera": "Billetera digital (Zelle, Pago Móvil, Binance…)", "otro": "Otro"}
ESQUEMA = """
CREATE TABLE IF NOT EXISTS erp_cuentas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL UNIQUE COLLATE NOCASE,
    banco TEXT,
    tipo TEXT NOT NULL DEFAULT 'banco',          -- banco / efectivo / billetera / otro
    moneda TEXT NOT NULL DEFAULT 'USD',          -- USD / VES
    numero TEXT,                                 -- solo los últimos 4 dígitos
    titular TEXT,
    alias TEXT,                                  -- otros nombres con los que llegan los comprobantes, separados por coma
    saldo_inicial REAL NOT NULL DEFAULT 0,
    fecha_saldo TEXT NOT NULL,                   -- desde esta fecha se cuentan los movimientos
    activa INTEGER NOT NULL DEFAULT 1,
    creada_por INTEGER REFERENCES usuarios(id),
    creada_en TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def asegurar():
    finanzas.asegurar()
    with db.conectar() as con:
        con.executescript(ESQUEMA)


def _hoy() -> str:
    return date.today().isoformat()


def _cuenta(con, cuenta_id) -> dict:
    c = con.execute("SELECT * FROM erp_cuentas WHERE id = ?", (cuenta_id,)).fetchone()
    if not c:
        raise ValueError("cuenta no encontrada")
    return dict(c)


def _alias(c: dict) -> list:
    return [a.strip().lower() for a in (c.get("alias") or "").split(",") if a.strip()]


def _conv(moneda: str, monto: float, tasa):
    if moneda == "USD":
        return round(monto, 2), (round(monto * tasa, 2) if tasa else None)
    return (round(monto / tasa, 2) if tasa else None), round(monto, 2)


def _tasa_vigente():
    t = db.tasa_vigente()
    return t["tasa"] if t["vigente"] else None


# ---------------------------------------------------------------- cuentas
def _datos_cuenta(d: dict, existente: dict | None = None) -> dict:
    nombre = re.sub(r"\s+", " ", str((existente["nombre"] if existente else d.get("nombre")) or "")).strip()
    if len(nombre) < 2 or len(nombre) > 60:
        raise ValueError("el nombre de la cuenta debe tener entre 2 y 60 caracteres")
    tipo = d.get("tipo") or (existente or {}).get("tipo") or "banco"
    if tipo not in TIPOS:
        raise ValueError("tipo de cuenta no válido")
    moneda = finanzas.ALIAS_MONEDA.get(finanzas._plano(existente["moneda"] if existente else d.get("moneda")))
    if not moneda:
        raise ValueError("la moneda de la cuenta es USD o VES")
    numero = re.sub(r"\D", "", str(d.get("numero") or ""))[-4:] or None
    saldo = d.get("saldo_inicial")
    saldo = 0.0 if saldo in (None, "") else finanzas._monto(saldo)
    return {"nombre": nombre, "banco": (str(d.get("banco") or "").strip()[:60] or None), "tipo": tipo, "moneda": moneda, "numero": numero,
            "titular": (str(d.get("titular") or "").strip()[:80] or None), "alias": (str(d.get("alias") or "").strip()[:200] or None),
            "saldo_inicial": round(saldo, 2), "fecha_saldo": finanzas._fecha_flexible(d.get("fecha_saldo"), _hoy())}


def crear_cuenta(d: dict, usuario_id: int) -> int:
    asegurar()
    v = _datos_cuenta(d)
    with db.conectar() as con:
        if con.execute("SELECT 1 FROM erp_cuentas WHERE nombre = ?", (v["nombre"],)).fetchone():
            raise ValueError("ya existe una cuenta con ese nombre")
        return con.execute(
            "INSERT INTO erp_cuentas (nombre, banco, tipo, moneda, numero, titular, alias, saldo_inicial, fecha_saldo, creada_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (v["nombre"], v["banco"], v["tipo"], v["moneda"], v["numero"], v["titular"], v["alias"], v["saldo_inicial"], v["fecha_saldo"], usuario_id)).lastrowid


def editar_cuenta(cuenta_id: int, d: dict):
    """El nombre y la moneda no cambian: los movimientos ya cargados se ligan por el nombre."""
    asegurar()
    with db.conectar() as con:
        actual = _cuenta(con, cuenta_id)
        v = _datos_cuenta({**actual, **{k: d[k] for k in d if k in ("banco", "tipo", "numero", "titular", "alias", "saldo_inicial", "fecha_saldo")}}, actual)
        con.execute("UPDATE erp_cuentas SET banco = ?, tipo = ?, numero = COALESCE(?, numero), titular = ?, alias = ?, saldo_inicial = ?, fecha_saldo = ?, activa = ? WHERE id = ?",
                    (v["banco"], v["tipo"], v["numero"], v["titular"], v["alias"], v["saldo_inicial"], v["fecha_saldo"], 0 if d.get("activa") in (False, 0, "0") else 1, cuenta_id))


# ---------------------------------------------------------------- movimientos de una cuenta
def _movs_cuenta(con, c: dict) -> list:
    """Todos los movimientos de la cuenta desde su fecha de saldo, en su moneda, ordenados. valor = None si falta la tasa."""
    filas = []
    for b in con.execute("SELECT * FROM erp_bancos WHERE COALESCE(anulado, 0) = 0 AND lower(cuenta) = lower(?) AND fecha >= ?", (c["nombre"], c["fecha_saldo"])).fetchall():
        v = b["monto"] if b["moneda"] == c["moneda"] else (b["monto_usd"] if c["moneda"] == "USD" else b["monto_bs"])
        filas.append({"fuente": "banco", "id": b["id"], "fecha": b["fecha"], "tipo": b["tipo"], "concepto": b["concepto"], "referencia": b["referencia"], "moneda": b["moneda"], "monto": b["monto"],
                      "valor": v, "conciliado": bool(b["conciliado"]), "categoria": b["categoria"], "transferencia": bool(b["transferencia_id"])})
    nombres = [c["nombre"].lower()] + _alias(c)
    marcas = ",".join("?" * len(nombres))
    for p in con.execute(
            "SELECT p.*, k.nombre AS cliente, k.telefono, cp.cuenta_destino FROM erp_pagos p JOIN contactos k ON k.id = p.contacto_id "
            f"JOIN comprobantes cp ON cp.id = p.comprobante_id WHERE lower(trim(cp.cuenta_destino)) IN ({marcas}) AND p.fecha >= ?", (*nombres, c["fecha_saldo"])).fetchall():
        v = p["monto_original"] if p["moneda"] == c["moneda"] else (p["monto_usd"] if c["moneda"] == "USD" else p["monto_bs"])
        filas.append({"fuente": "pedido", "id": p["id"], "fecha": p["fecha"], "tipo": "ingreso", "concepto": f"Pago de pedido · {p['cliente'] or p['telefono']}", "referencia": p["referencia"],
                      "moneda": p["moneda"], "monto": p["monto_original"], "valor": v, "conciliado": bool(p["conciliado"]), "categoria": finanzas.VENTAS_SISTEMA, "transferencia": False})
    filas.sort(key=lambda m: (m["fecha"], 0 if m["fuente"] == "banco" else 1, m["id"]))
    return filas


def _saldos(c: dict, filas: list) -> dict:
    firma = lambda m: (m["valor"] if m["tipo"] == "ingreso" else -m["valor"]) if m["valor"] is not None else 0.0
    return {"saldo": round(c["saldo_inicial"] + sum(firma(m) for m in filas), 2),
            "saldo_conciliado": round(c["saldo_inicial"] + sum(firma(m) for m in filas if m["conciliado"]), 2),
            "sin_conciliar": sum(1 for m in filas if not m["conciliado"]), "sin_tasa": sum(1 for m in filas if m["valor"] is None), "movimientos": len(filas)}


def panel() -> dict:
    asegurar()
    tasa = _tasa_vigente()
    with db.conectar() as con:
        cuentas = [dict(c) for c in con.execute("SELECT * FROM erp_cuentas ORDER BY activa DESC, nombre").fetchall()]
        nombres = {c["nombre"].lower() for c in cuentas}
        asignables = set(nombres)
        for c in cuentas:
            asignables |= set(_alias(c))
        for c in cuentas:
            c.update(_saldos(c, _movs_cuenta(con, c)))
            c["saldo_usd"] = round(c["saldo"] if c["moneda"] == "USD" else (c["saldo"] / tasa if tasa else 0), 2)
            c["usd_estimado"] = c["moneda"] == "VES"
        usadas = [r["cuenta"] for r in con.execute("SELECT cuenta, COUNT(*) AS n FROM erp_bancos WHERE COALESCE(anulado, 0) = 0 GROUP BY cuenta").fetchall()]
        sin_registrar = sorted({u for u in usadas if u.lower() not in nombres}, key=str.lower)
        pagos = con.execute("SELECT p.id, p.monto_usd, lower(trim(COALESCE(cp.cuenta_destino, ''))) AS d FROM erp_pagos p LEFT JOIN comprobantes cp ON cp.id = p.comprobante_id").fetchall()
        sin_cuenta = [p for p in pagos if p["d"] not in asignables]
        pend = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(monto_usd), 0) AS t FROM erp_compras WHERE forma_pago = 'pendiente' AND id NOT IN "
                           "(SELECT compra_id FROM erp_bancos WHERE compra_id IS NOT NULL AND COALESCE(anulado, 0) = 0)").fetchone()
    activas = [c for c in cuentas if c["activa"]]
    return {"cuentas": cuentas, "total_usd": round(sum(c["saldo_usd"] for c in activas), 2), "tasa": tasa,
            "sin_conciliar": sum(c["sin_conciliar"] for c in activas), "sin_registrar": sin_registrar,
            "pagos_sin_cuenta": {"n": len(sin_cuenta), "usd": round(sum(p["monto_usd"] or 0 for p in sin_cuenta), 2)},
            "compras_pendientes": {"n": pend["n"], "usd": round(pend["t"], 2)}, "tipos": TIPOS}


def movimientos_cuenta(cuenta_id: int, desde=None, hasta=None) -> dict:
    asegurar()
    with db.conectar() as con:
        c = _cuenta(con, cuenta_id)
        filas = _movs_cuenta(con, c)
    d = finanzas._fecha_flexible(desde, c["fecha_saldo"]) if desde else c["fecha_saldo"]
    h = finanzas._fecha_flexible(hasta, _hoy()) if hasta else "9999-12-31"
    saldo, salida, previo = c["saldo_inicial"], [], c["saldo_inicial"]
    for m in filas:
        saldo += (m["valor"] if m["tipo"] == "ingreso" else -m["valor"]) if m["valor"] is not None else 0
        if m["fecha"] < d:
            previo = saldo
        elif m["fecha"] <= h:
            salida.append({**m, "saldo": round(saldo, 2)})
    return {"cuenta": {**c, **_saldos(c, filas)}, "desde": d, "hasta": h if h != "9999-12-31" else None, "saldo_previo": round(previo, 2), "filas": salida[::-1]}


# ---------------------------------------------------------------- transferencias
def transferencia(d: dict, usuario_id: int) -> dict:
    asegurar()
    fecha = finanzas._fecha_flexible(d.get("fecha"), _hoy())
    monto = abs(finanzas._monto(d.get("monto")))
    if monto <= 0:
        raise ValueError("el monto debe ser mayor que cero")
    com = abs(finanzas._monto(d.get("comision"))) if d.get("comision") not in (None, "") else 0.0
    ref = (str(d.get("referencia") or "").strip()[:60] or None)
    tid = uuid.uuid4().hex
    with db.conectar() as con:
        o, de = _cuenta(con, d.get("origen_id")), _cuenta(con, d.get("destino_id"))
        if o["id"] == de["id"]:
            raise ValueError("el origen y el destino son la misma cuenta")
        if not (o["activa"] and de["activa"]):
            raise ValueError("una de las cuentas está desactivada")
        tasa = contable._num(d.get("tasa"), "la tasa") if d.get("tasa") not in (None, "") else _tasa_vigente()
        if o["moneda"] == de["moneda"]:
            destino = monto
        else:
            if not tasa:
                raise ValueError("para pasar de dólares a bolívares (o al revés) hace falta la tasa: cárgala o escríbela")
            destino = abs(finanzas._monto(d.get("monto_destino"))) if d.get("monto_destino") not in (None, "") else round(monto * tasa if o["moneda"] == "USD" else monto / tasa, 2)
        filas = [(fecha, o["nombre"], "egreso", f"Transferencia a {de['nombre']}", o["moneda"], monto, "transferencia"),
                 (fecha, de["nombre"], "ingreso", f"Transferencia desde {o['nombre']}", de["moneda"], destino, "transferencia")]
        if com:
            filas.append((fecha, o["nombre"], "egreso", f"Comisión de la transferencia a {de['nombre']}", o["moneda"], com, "comisiones_bancarias"))
        for f, cuenta, tipo, concepto, moneda, m, cat in filas:
            usd, bs = _conv(moneda, m, tasa)
            con.execute("INSERT INTO erp_bancos (fecha, cuenta, tipo, concepto, moneda, monto, tasa, monto_usd, monto_bs, referencia, categoria, origen, transferencia_id, registrado_por) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'transferencia', ?, ?)", (f, cuenta, tipo, concepto, moneda, m, tasa, usd, bs, ref, cat, tid, usuario_id))
    return {"transferencia_id": tid, "origen": o["nombre"], "destino": de["nombre"], "monto": monto, "monto_destino": destino}


# ---------------------------------------------------------------- compras pendientes
def compras_pendientes() -> list:
    asegurar()
    with db.conectar() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, fecha, proveedor, tipo, doc_tipo, numero, moneda, monto, monto_usd FROM erp_compras WHERE forma_pago = 'pendiente' AND id NOT IN "
            "(SELECT compra_id FROM erp_bancos WHERE compra_id IS NOT NULL AND COALESCE(anulado, 0) = 0) ORDER BY fecha, id").fetchall()]


def pagar_compra(compra_id: int, cuenta_id: int, fecha, referencia, usuario_id: int) -> int:
    """Paga una compra pendiente desde una cuenta: crea el egreso ligado a la compra, que deja de figurar como pendiente."""
    asegurar()
    fecha = finanzas._fecha_flexible(fecha, _hoy())
    with db.conectar() as con:
        c = con.execute("SELECT * FROM erp_compras WHERE id = ?", (compra_id,)).fetchone()
        if not c:
            raise ValueError("compra no encontrada")
        if c["forma_pago"] != "pendiente" or con.execute("SELECT 1 FROM erp_bancos WHERE compra_id = ? AND COALESCE(anulado, 0) = 0", (compra_id,)).fetchone():
            raise ValueError("esa compra ya está pagada")
        cu = _cuenta(con, cuenta_id)
        if not cu["activa"]:
            raise ValueError("la cuenta está desactivada")
        cat = {"mercancia": "mercancia", "servicio": "servicios"}.get(c["tipo"], "otros_egresos")
        tasa = c["tasa"] or _tasa_vigente()
        usd, bs = _conv(c["moneda"], c["monto"], tasa)
        mid = con.execute(
            "INSERT INTO erp_bancos (fecha, cuenta, tipo, concepto, moneda, monto, tasa, monto_usd, monto_bs, referencia, categoria, origen, compra_id, registrado_por) "
            "VALUES (?, ?, 'egreso', ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)",
            (fecha, cu["nombre"], f"Pago a {c['proveedor']}" + (f" · {c['numero']}" if c["numero"] else ""), c["moneda"], c["monto"], tasa, usd, bs,
             (str(referencia or "").strip()[:60] or None), cat, compra_id, usuario_id)).lastrowid
        con.execute("UPDATE erp_compras SET forma_pago = ? WHERE id = ?", ("caja_chica" if cu["tipo"] == "efectivo" else "banco", compra_id))
        return mid


# ---------------------------------------------------------------- conciliación
def _ref(s) -> str:
    return re.sub(r"\s", "", str(s or "")).lstrip("0").lower()


def conciliar_analizar(cuenta_id: int, texto: str) -> dict:
    """Cruza el estado de cuenta (pegado o CSV) con los movimientos aún sin conciliar de la cuenta. No guarda nada."""
    asegurar()
    with db.conectar() as con:
        c = _cuenta(con, cuenta_id)
        movs = _movs_cuenta(con, c)
    lote = finanzas.lote_analizar(texto, {"moneda": c["moneda"], "cuenta": c["nombre"]})
    filas = [f for f in lote["filas"] if not f["errores"]]
    errores = [{"n": f["n"], "error": "; ".join(f["errores"])} for f in lote["filas"] if f["errores"]]
    cand = [m for m in movs if not m["conciliado"] and m["valor"] is not None]
    usados, coincidencias, solo_banco = set(), [], []
    dia = lambda s: datetime.strptime(s, "%Y-%m-%d").date()
    for f in sorted(filas, key=lambda x: x["fecha"]):
        mejor, mejor_p = None, None
        for m in cand:
            clave = (m["fuente"], m["id"])
            if clave in usados or m["tipo"] != f["tipo"] or abs(m["valor"] - f["monto"]) > 0.009:
                continue
            dd = abs((dia(m["fecha"]) - dia(f["fecha"])).days)
            misma_ref = bool(_ref(m["referencia"])) and _ref(m["referencia"]) == _ref(f["referencia"])
            if not misma_ref and dd > 3:
                continue
            p = (0 if misma_ref else 1, dd)
            if mejor is None or p < mejor_p:
                mejor, mejor_p = m, p
        if mejor:
            usados.add((mejor["fuente"], mejor["id"]))
            coincidencias.append({"n": f["n"], "fecha": f["fecha"], "tipo": f["tipo"], "concepto": f["concepto"], "monto": f["monto"], "referencia": f["referencia"],
                                  "fuente": mejor["fuente"], "id": mejor["id"], "concepto_sistema": mejor["concepto"], "fecha_sistema": mejor["fecha"], "por_referencia": mejor_p[0] == 0})
        else:
            solo_banco.append({k: f[k] for k in ("n", "fecha", "tipo", "concepto", "monto", "moneda", "cuenta", "referencia")})
    if filas:
        d0, d1 = dia(min(f["fecha"] for f in filas)) - timedelta(days=3), dia(max(f["fecha"] for f in filas)) + timedelta(days=3)
        solo_sistema = [m for m in cand if (m["fuente"], m["id"]) not in usados and d0 <= dia(m["fecha"]) <= d1]
    else:
        solo_sistema = []
    return {"cuenta": c["nombre"], "moneda": c["moneda"], "lote_texto_filas": len(lote["filas"]), "coincidencias": coincidencias, "solo_banco": solo_banco,
            "solo_sistema": solo_sistema, "errores": errores, "resumen": {"filas": len(lote["filas"]), "coinciden": len(coincidencias), "solo_banco": len(solo_banco),
                                                                           "solo_sistema": len(solo_sistema), "con_error": len(errores)}}


def conciliar_aplicar(cuenta_id: int, pares: list, saldo_extracto=None) -> dict:
    asegurar()
    if not isinstance(pares, list) or not pares:
        raise ValueError("no hay coincidencias para conciliar")
    saldo_ext = None if saldo_extracto in (None, "") else finanzas._monto(saldo_extracto)
    with db.conectar() as con:
        c = _cuenta(con, cuenta_id)
        validos = {(m["fuente"], m["id"]) for m in _movs_cuenta(con, c) if not m["conciliado"]}
        n = 0
        for p in pares:
            clave = (p.get("fuente"), int(p.get("id") or 0)) if isinstance(p, dict) else None
            if clave in validos:
                validos.discard(clave)
                tabla = "erp_bancos" if clave[0] == "banco" else "erp_pagos"
                con.execute(f"UPDATE {tabla} SET conciliado = 1, conciliado_en = CURRENT_TIMESTAMP WHERE id = ?", (clave[1],))
                n += 1
        s = _saldos(c, _movs_cuenta(con, c))
    r = {"conciliadas": n, "saldo_conciliado": s["saldo_conciliado"], "saldo_sistema": s["saldo"], "sin_conciliar": s["sin_conciliar"]}
    if saldo_ext is not None:
        r["saldo_extracto"] = round(saldo_ext, 2)
        r["diferencia"] = round(saldo_ext - s["saldo_conciliado"], 2)
    return r


def conciliar_marcar(fuente: str, mov_id: int, valor: bool):
    asegurar()
    if fuente not in ("banco", "pedido"):
        raise ValueError("fuente no válida")
    with db.conectar() as con:
        tabla = "erp_bancos" if fuente == "banco" else "erp_pagos"
        cur = con.execute(f"UPDATE {tabla} SET conciliado = ?, conciliado_en = {'CURRENT_TIMESTAMP' if valor else 'NULL'} WHERE id = ?", (1 if valor else 0, mov_id))
        if cur.rowcount == 0:
            raise ValueError("movimiento no encontrado")
