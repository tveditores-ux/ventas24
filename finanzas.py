"""
Ingresos y egresos: carga manual, carga por lote y el tablero de la página de inicio del ERP.

Todo movimiento manual vive en erp_bancos (el mismo libro de «Bancos y caja chica» que lee la contadora), con categoría,
origen (manual / lote) y anulación. Un movimiento no se borra: se anula con motivo y deja de sumar, pero queda a la vista.

El tablero junta tres fuentes sin contar nada dos veces:
  - pagos de pedidos confirmados (erp_pagos): se suman solos, por eso NO se cargan a mano;
  - movimientos manuales o por lote (erp_bancos);
  - compras y gastos pagados que no tienen su movimiento de banco asociado (erp_compras).
Los aportes, préstamos y retiros de socios son movimientos de capital: se muestran aparte y no inflan ni bajan el resultado.
"""

import csv
import io
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta

import contable
import db

CAT_INGRESO = {
    "venta_directa": ("Venta directa (fuera del sistema)", False),
    "otros_ingresos": ("Otros ingresos", False),
    "devolucion": ("Devolución de proveedor", False),
    "aporte_socio": ("Aporte de socio", True),
    "prestamo": ("Préstamo recibido", True),
}
CAT_EGRESO = {
    "mercancia": ("Compra de mercancía", False),
    "importacion": ("Importación y aduana", False),
    "nomina": ("Nómina y comisiones", False),
    "alquiler": ("Alquiler", False),
    "servicios": ("Servicios (luz, agua, internet, teléfono)", False),
    "transporte": ("Transporte y fletes", False),
    "impuestos": ("Impuestos y tasas", False),
    "comisiones_bancarias": ("Comisiones bancarias", False),
    "mantenimiento": ("Mantenimiento", False),
    "publicidad": ("Publicidad y marketing", False),
    "otros_egresos": ("Otros egresos", False),
    "retiro_socio": ("Retiro de socio", True),
}
VENTAS_SISTEMA = "ventas_sistema"
INTERNAS = {"transferencia"}          # pasa dinero de una cuenta a otra: no es ingreso ni egreso del negocio
SIN_CATEGORIA = "sin_categoria"
MAX_FILAS = 2000

ALIAS_TIPO = {"ingreso": "ingreso", "ingresos": "ingreso", "entrada": "ingreso", "abono": "ingreso", "cobro": "ingreso", "in": "ingreso", "+": "ingreso",
              "egreso": "egreso", "egresos": "egreso", "salida": "egreso", "gasto": "egreso", "pago": "egreso", "cargo": "egreso", "compra": "egreso", "out": "egreso", "-": "egreso"}
ALIAS_MONEDA = {"usd": "USD", "$": "USD", "us$": "USD", "dolar": "USD", "dolares": "USD", "divisa": "USD", "divisas": "USD",
                "ves": "VES", "bs": "VES", "bs.": "VES", "bsd": "VES", "bolivar": "VES", "bolivares": "VES"}
COLUMNAS = {
    "fecha": ("fecha", "date", "fecha operacion", "fecha de operacion"),
    "tipo": ("tipo", "movimiento", "tipo de movimiento"),
    "concepto": ("concepto", "descripcion", "detalle", "concept", "observacion"),
    "monto": ("monto", "importe", "valor", "amount"),
    "ingreso": ("ingreso", "ingresos", "entrada", "entradas", "abono", "abonos", "credito", "haber"),
    "egreso": ("egreso", "egresos", "salida", "salidas", "cargo", "cargos", "debito", "debe"),
    "moneda": ("moneda", "divisa", "currency"),
    "cuenta": ("cuenta", "banco", "caja"),
    "categoria": ("categoria", "rubro"),
    "referencia": ("referencia", "ref", "nro", "numero", "nro ref"),
    "tasa": ("tasa", "tasa bcv"),
}


def asegurar():
    contable.asegurar_tablas()
    with db.conectar() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS erp_lotes_finanzas (
                lote_id TEXT PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                filas INTEGER NOT NULL,
                resultado TEXT,
                fecha TEXT DEFAULT CURRENT_TIMESTAMP
            );""")


def categorias() -> dict:
    return {"ingreso": [{"clave": k, "nombre": v[0], "capital": v[1]} for k, v in CAT_INGRESO.items()],
            "egreso": [{"clave": k, "nombre": v[0], "capital": v[1]} for k, v in CAT_EGRESO.items()]}


def _plano(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


def _es_capital(tipo: str, categoria: str) -> bool:
    return (CAT_INGRESO if tipo == "ingreso" else CAT_EGRESO).get(categoria, ("", False))[1]


def _nombre_categoria(tipo: str, categoria) -> str:
    if categoria == VENTAS_SISTEMA:
        return "Ventas cobradas (pedidos)"
    if categoria in INTERNAS:
        return "Transferencia entre cuentas"
    return (CAT_INGRESO if tipo == "ingreso" else CAT_EGRESO).get(categoria, ("Sin categoría", False))[0]


# ---------------------------------------------------------------- lectura de datos sueltos
def _monto(x) -> float:
    s = re.sub(r"(?i)(us\$|bs\.?|usd|ves|\$|\s)", "", str(x if x is not None else ""))
    if not s:
        raise ValueError("falta el monto")
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-+()")
    if not re.fullmatch(r"[\d.,]+", s):
        raise ValueError(f"monto no válido: «{x}»")
    puntos, comas = s.count("."), s.count(",")
    if puntos and comas:                                   # 1.234,56  ó  1,234.56: el último separador es el decimal
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif puntos + comas:
        sep = "." if puntos else ","
        partes = s.split(sep)
        if len(partes) > 2 or (len(partes[-1]) == 3 and 1 <= len(partes[0]) <= 3 and not partes[0].startswith("0")):
            s = "".join(partes)                            # separador de miles: 1.500  ó  1,500
        else:
            s = ".".join(partes)
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"monto no válido: «{x}»")
    return -v if neg else v


def _fecha_flexible(x, defecto=None) -> str:
    s = str(x or "").strip()
    if not s:
        if defecto:
            return defecto
        raise ValueError("falta la fecha")
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10] if f.startswith("%Y-") else s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"fecha no válida: «{s}» (usa AAAA-MM-DD o DD/MM/AAAA)")


def _categoria(tipo: str, valor) -> str:
    cats = CAT_INGRESO if tipo == "ingreso" else CAT_EGRESO
    v = _plano(valor)
    if not v:
        return "otros_ingresos" if tipo == "ingreso" else "otros_egresos"
    for k, (nombre, _) in cats.items():
        if v in (k, _plano(nombre), k.replace("_", " ")):
            return k
    if len(v) >= 4:
        parecidas = [k for k, (nombre, _) in cats.items() if _plano(nombre).startswith(v) or v in _plano(nombre)]
        if len(parecidas) == 1:
            return parecidas[0]
        if parecidas:
            raise ValueError(f"categoría «{valor}» ambigua: puede ser " + " o ".join(f"«{cats[k][0]}»" for k in parecidas))
    raise ValueError(f"categoría «{valor}» desconocida para un {tipo}")


def normalizar(d: dict, exigir_fecha: bool = False) -> dict:
    """Valida un movimiento y le calcula el equivalente en USD y Bs. Lanza ValueError con el motivo en español."""
    tipo = ALIAS_TIPO.get(_plano(d.get("tipo")))
    if tipo is None:
        raise ValueError("el tipo debe ser ingreso o egreso")
    concepto = re.sub(r"\s+", " ", str(d.get("concepto") or "")).strip()
    if len(concepto) < 2:
        raise ValueError("falta el concepto")
    if len(concepto) > 200:
        raise ValueError("el concepto es muy largo (máximo 200 caracteres)")
    cuenta = re.sub(r"\s+", " ", str(d.get("cuenta") or "")).strip()
    if not cuenta:
        raise ValueError("falta la cuenta (banco, Zelle, efectivo o «Caja chica»)")
    moneda = ALIAS_MONEDA.get(_plano(d.get("moneda") or "USD"))
    if moneda is None:
        raise ValueError(f"moneda no válida: «{d.get('moneda')}» (USD o VES)")
    monto = abs(_monto(d.get("monto")))
    if monto <= 0:
        raise ValueError("el monto debe ser mayor que cero")
    if monto > 1e12:
        raise ValueError("el monto es demasiado grande")
    tasa = contable._num(d.get("tasa"), "la tasa") if d.get("tasa") not in (None, "") else None
    if tasa is not None and tasa <= 0:
        raise ValueError("la tasa debe ser mayor que cero")
    tasa = tasa or (db.tasa_vigente()["tasa"] if db.tasa_vigente()["vigente"] else None)
    if moneda == "USD":
        usd, bs = round(monto, 2), (round(monto * tasa, 2) if tasa else None)
    else:
        usd, bs = (round(monto / tasa, 2) if tasa else None), round(monto, 2)
    return {"fecha": _fecha_flexible(d.get("fecha"), None if exigir_fecha else date.today().isoformat()), "tipo": tipo, "concepto": concepto,
            "cuenta": cuenta[:60], "moneda": moneda, "monto": round(monto, 2), "tasa": tasa, "monto_usd": usd, "monto_bs": bs,
            "categoria": _categoria(tipo, d.get("categoria")), "referencia": (str(d.get("referencia") or "").strip()[:60] or None)}


def _clave(m: dict) -> tuple:
    return (m["fecha"], m["tipo"], m["cuenta"].lower(), m["moneda"], round(m["monto"], 2), (m["referencia"] or m["concepto"]).lower())


def _claves_existentes(con, desde: str, hasta: str) -> set:
    return {(r["fecha"], r["tipo"], r["cuenta"].lower(), r["moneda"], round(r["monto"], 2), (r["referencia"] or r["concepto"]).lower())
            for r in con.execute("SELECT fecha, tipo, cuenta, moneda, monto, referencia, concepto FROM erp_bancos "
                                 "WHERE COALESCE(anulado, 0) = 0 AND fecha BETWEEN ? AND ?", (desde, hasta)).fetchall()}


# ---------------------------------------------------------------- carga manual
def registrar(d: dict, usuario_id: int, origen: str = "manual", lote_id: str | None = None) -> int:
    asegurar()
    m = normalizar(d)
    with db.conectar() as con:
        return _insertar(con, m, usuario_id, origen, lote_id)


def _insertar(con, m: dict, usuario_id: int, origen: str, lote_id) -> int:
    return con.execute(
        "INSERT INTO erp_bancos (fecha, cuenta, tipo, concepto, moneda, monto, tasa, monto_usd, monto_bs, referencia, categoria, origen, lote_id, registrado_por) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (m["fecha"], m["cuenta"], m["tipo"], m["concepto"], m["moneda"], m["monto"], m["tasa"], m["monto_usd"], m["monto_bs"], m["referencia"],
         m["categoria"], origen, lote_id, usuario_id)).lastrowid


def anular(mov_id: int, motivo: str, usuario_id: int):
    asegurar()
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise ValueError("escribe el motivo de la anulación (mínimo 5 caracteres)")
    with db.conectar() as con:
        r = con.execute("SELECT id, transferencia_id, COALESCE(anulado, 0) AS a FROM erp_bancos WHERE id = ?", (mov_id,)).fetchone()
        if not r:
            raise ValueError("movimiento no encontrado")
        if r["a"]:
            raise ValueError("ese movimiento ya está anulado")
        # una transferencia son dos (o tres) movimientos: se anulan juntos para que los saldos no queden descuadrados
        donde, arg = ("transferencia_id = ?", r["transferencia_id"]) if r["transferencia_id"] else ("id = ?", mov_id)
        con.execute(f"UPDATE erp_bancos SET anulado = 1, anulado_motivo = ?, anulado_por = ?, anulado_en = CURRENT_TIMESTAMP WHERE {donde}", (motivo, usuario_id, arg))


# ---------------------------------------------------------------- carga por lote
def _delimitador(muestra: str) -> str:
    primera = muestra.splitlines()[0] if muestra.splitlines() else ""
    return max(("\t", ";", ","), key=lambda s: primera.count(s))


def _mapa_columnas(cabecera: list) -> dict:
    mapa = {}
    for i, c in enumerate(cabecera):
        p = _plano(c)
        for campo, alias in COLUMNAS.items():
            if p in alias and campo not in mapa:
                mapa[campo] = i
                break
    return mapa


def lote_analizar(texto: str, defecto: dict | None = None) -> dict:
    """Lee un lote pegado desde Excel o un CSV y lo revisa fila por fila SIN guardar nada."""
    asegurar()
    defecto = defecto or {}
    texto = (texto or "").lstrip("﻿").strip()
    if not texto:
        raise ValueError("pega las filas o sube un archivo CSV")
    filas_crudas = list(csv.reader(io.StringIO(texto), delimiter=_delimitador(texto)))
    filas_crudas = [f for f in filas_crudas if any(c.strip() for c in f)]
    if len(filas_crudas) < 2:
        raise ValueError("el lote necesita una fila de títulos y al menos un movimiento")
    mapa = _mapa_columnas(filas_crudas[0])
    if "fecha" not in mapa or not (("monto" in mapa) or ("ingreso" in mapa and "egreso" in mapa)) or "concepto" not in mapa:
        raise ValueError("la primera fila debe tener los títulos. Mínimo: fecha, concepto y monto (o dos columnas: ingreso y egreso). "
                         "Descarga la plantilla para ver el formato.")
    datos = filas_crudas[1:]
    if len(datos) > MAX_FILAS:
        raise ValueError(f"el lote tiene {len(datos)} filas; el máximo es {MAX_FILAS}. Divídelo en partes")
    salida, vistas = [], {}
    fechas = []
    for n, f in enumerate(datos, start=2):
        g = lambda campo: (f[mapa[campo]].strip() if campo in mapa and mapa[campo] < len(f) else "")
        fila = {"n": n, "errores": [], "avisos": []}
        try:
            d = {"fecha": g("fecha"), "concepto": g("concepto"), "moneda": g("moneda") or defecto.get("moneda") or "USD",
                 "cuenta": g("cuenta") or defecto.get("cuenta") or "", "categoria": g("categoria"), "referencia": g("referencia"), "tasa": g("tasa")}
            if "ingreso" in mapa and "egreso" in mapa and "monto" not in mapa:
                i_, e_ = g("ingreso"), g("egreso")
                vi, ve = (_monto(i_) if i_ else 0), (_monto(e_) if e_ else 0)
                if vi and ve:
                    raise ValueError("tiene ingreso y egreso en la misma fila")
                if not (vi or ve):
                    raise ValueError("no tiene monto")
                d["tipo"], d["monto"] = ("ingreso", vi) if vi else ("egreso", ve)
            else:
                v = _monto(g("monto"))
                d["tipo"] = g("tipo") or defecto.get("tipo") or ("ingreso" if v >= 0 else "egreso")
                d["monto"] = v
            m = normalizar(d, exigir_fecha=True)
            fila.update(m)
            fechas.append(m["fecha"])
            k = _clave(m)
            if k in vistas:
                (fila["avisos"] if not m["referencia"] else fila["errores"]).append(
                    f"se repite la fila {vistas[k]} del archivo" if not m["referencia"] else f"misma referencia que la fila {vistas[k]}")
            vistas.setdefault(k, n)
            if m["monto_usd"] is None:
                fila["avisos"].append("sin tasa cargada: no entra en los totales en dólares hasta que haya tasa")
        except ValueError as e:
            fila["errores"].append(str(e))
            fila.update({"concepto": g("concepto"), "monto": g("monto") or g("ingreso") or g("egreso"), "fecha": g("fecha"), "tipo": g("tipo"),
                         "cuenta": g("cuenta") or defecto.get("cuenta") or "", "moneda": g("moneda") or defecto.get("moneda") or "", "categoria": g("categoria"),
                         "referencia": g("referencia"), "monto_usd": None})
        salida.append(fila)
    if fechas:
        with db.conectar() as con:
            existentes = _claves_existentes(con, min(fechas), max(fechas))
        for fila in salida:
            if not fila["errores"] and _clave(fila) in existentes:
                fila["duplicado"] = True
                fila["avisos"].append("ya existe en el sistema (misma fecha, cuenta, monto y referencia)")
    ok = [f for f in salida if not f["errores"]]
    return {"lote_id": uuid.uuid4().hex, "columnas": {k: filas_crudas[0][i] for k, i in mapa.items()}, "filas": salida,
            "resumen": {"total": len(salida), "validas": len(ok), "con_error": len(salida) - len(ok), "duplicadas": sum(1 for f in ok if f.get("duplicado")),
                        "ingresos_usd": round(sum(f["monto_usd"] or 0 for f in ok if f["tipo"] == "ingreso"), 2),
                        "egresos_usd": round(sum(f["monto_usd"] or 0 for f in ok if f["tipo"] == "egreso"), 2)}}


def lote_aplicar(lote_id: str, filas: list, usuario_id: int, omitir_duplicados: bool = True) -> dict:
    """Guarda un lote ya revisado. Todo o nada: si una fila no pasa la validación, no se guarda ninguna.
    El lote_id evita aplicar dos veces el mismo lote si se reintenta el envío."""
    asegurar()
    if not re.fullmatch(r"[0-9a-f]{32}", str(lote_id or "")):
        raise ValueError("lote no válido: vuelve a analizar el archivo")
    if not isinstance(filas, list) or not filas:
        raise ValueError("no hay filas para guardar")
    if len(filas) > MAX_FILAS:
        raise ValueError(f"máximo {MAX_FILAS} filas por lote")
    movs, errores = [], []
    for i, f in enumerate(filas, start=1):
        try:
            movs.append(normalizar(f if isinstance(f, dict) else {}, exigir_fecha=True))
        except ValueError as e:
            errores.append(f"fila {f.get('n', i) if isinstance(f, dict) else i}: {e}")
    if errores:
        raise ValueError("no se guardó nada. Corrige estas filas: " + "; ".join(errores[:8]) + ("…" if len(errores) > 8 else ""))
    with db.conectar() as con:
        previo = con.execute("SELECT filas, resultado FROM erp_lotes_finanzas WHERE lote_id = ?", (lote_id,)).fetchone()
        if previo:
            return {"repetido": True, "guardadas": previo["filas"], "omitidas": 0, "mensaje": "Este lote ya se había guardado; no se duplicó."}
        existentes = _claves_existentes(con, min(m["fecha"] for m in movs), max(m["fecha"] for m in movs)) if omitir_duplicados else set()
        guardadas = omitidas = 0
        vistas = set()
        for m in movs:
            k = _clave(m)
            if omitir_duplicados and (k in existentes or (m["referencia"] and k in vistas)):
                omitidas += 1
                continue
            vistas.add(k)
            _insertar(con, m, usuario_id, "lote", lote_id)
            guardadas += 1
        con.execute("INSERT INTO erp_lotes_finanzas (lote_id, usuario_id, filas, resultado) VALUES (?, ?, ?, ?)", (lote_id, usuario_id, guardadas, f"omitidas {omitidas}"))
    return {"repetido": False, "guardadas": guardadas, "omitidas": omitidas,
            "mensaje": ("Se guardó 1 movimiento" if guardadas == 1 else f"Se guardaron {guardadas} movimientos")
            + ((" y se omitió 1 que ya existía." if omitidas == 1 else f" y se omitieron {omitidas} que ya existían.") if omitidas else ".")}


def plantilla_csv() -> str:
    b = io.StringIO()
    w = csv.writer(b, delimiter=";")
    w.writerow(["fecha", "tipo", "concepto", "monto", "moneda", "cuenta", "categoria", "referencia"])
    w.writerow(["2026-11-02", "ingreso", "Venta de mostrador factura 0001", "350,00", "USD", "Caja USD", "Venta directa", ""])
    w.writerow(["2026-11-03", "egreso", "Pago de alquiler del local", "180,00", "USD", "Zelle", "Alquiler", "ZL-5521"])
    w.writerow(["03/11/2026", "egreso", "Comisión bancaria", "1.250,50", "VES", "Banesco", "Comisiones bancarias", "00123456"])
    return "﻿" + b.getvalue()


# ---------------------------------------------------------------- tablero
def _movimientos(desde: str, hasta: str) -> list:
    """Todos los movimientos del período, de las tres fuentes y sin duplicar."""
    asegurar()
    out = []
    with db.conectar() as con:
        for p in con.execute("SELECT p.id, p.fecha, p.moneda, p.monto_original, p.monto_usd, p.metodo, c.nombre, c.telefono, p.referencia "
                             "FROM erp_pagos p JOIN contactos c ON c.id = p.contacto_id WHERE p.fecha BETWEEN ? AND ?", (desde, hasta)).fetchall():
            out.append({"fuente": "pedido", "id": p["id"], "fecha": p["fecha"], "tipo": "ingreso", "categoria": VENTAS_SISTEMA, "concepto": f"Pago de pedido · {p['nombre'] or p['telefono']}",
                        "cuenta": "Pagos de pedidos", "moneda": p["moneda"], "monto": p["monto_original"], "monto_usd": p["monto_usd"], "referencia": p["referencia"], "anulable": False})
        for b in con.execute("SELECT * FROM erp_bancos WHERE COALESCE(anulado, 0) = 0 AND fecha BETWEEN ? AND ?", (desde, hasta)).fetchall():
            out.append({"fuente": "banco", "id": b["id"], "fecha": b["fecha"], "tipo": b["tipo"], "categoria": b["categoria"] or SIN_CATEGORIA, "concepto": b["concepto"],
                        "cuenta": b["cuenta"], "moneda": b["moneda"], "monto": b["monto"], "monto_usd": b["monto_usd"], "referencia": b["referencia"],
                        "origen": b["origen"] or "manual", "anulable": True})
        for c in con.execute("SELECT * FROM erp_compras WHERE forma_pago IN ('banco', 'caja_chica') AND fecha BETWEEN ? AND ? AND id NOT IN "
                             "(SELECT compra_id FROM erp_bancos WHERE compra_id IS NOT NULL AND COALESCE(anulado, 0) = 0)", (desde, hasta)).fetchall():
            cat = {"mercancia": "mercancia", "servicio": "servicios"}.get(c["tipo"], "otros_egresos")
            out.append({"fuente": "compra", "id": c["id"], "fecha": c["fecha"], "tipo": "egreso", "categoria": cat, "concepto": f"{c['proveedor']}" + (f" · {c['nota']}" if c["nota"] else ""),
                        "cuenta": "Compras y gastos", "moneda": c["moneda"], "monto": c["monto"], "monto_usd": c["monto_usd"], "referencia": c["numero"], "anulable": False})
    return out


def _totales(movs: list) -> dict:
    t = {"ingresos_usd": 0.0, "egresos_usd": 0.0, "ventas_usd": 0.0, "capital_in_usd": 0.0, "capital_out_usd": 0.0, "sin_conversion": 0}
    for m in movs:
        if m["categoria"] in INTERNAS:
            continue
        if m["monto_usd"] is None:
            t["sin_conversion"] += 1
            continue
        capital = _es_capital(m["tipo"], m["categoria"])
        if capital:
            t["capital_in_usd" if m["tipo"] == "ingreso" else "capital_out_usd"] += m["monto_usd"]
        elif m["tipo"] == "ingreso":
            t["ingresos_usd"] += m["monto_usd"]
            if m["categoria"] == VENTAS_SISTEMA:
                t["ventas_usd"] += m["monto_usd"]
        else:
            t["egresos_usd"] += m["monto_usd"]
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}


def _periodo_anterior(d0: date, d1: date) -> tuple:
    dias = (d1 - d0).days + 1
    return d0 - timedelta(days=dias), d0 - timedelta(days=1)


def resumen(desde: str | None = None, hasta: str | None = None) -> dict:
    hoy = date.today()
    d1 = datetime.strptime(_fecha_flexible(hasta, hoy.isoformat()), "%Y-%m-%d").date()
    d0 = datetime.strptime(_fecha_flexible(desde, hoy.replace(day=1).isoformat()), "%Y-%m-%d").date()
    if d0 > d1:
        raise ValueError("la fecha inicial es posterior a la final")
    if (d1 - d0).days > 3700:
        raise ValueError("el período es demasiado largo (máximo 10 años)")
    movs = _movimientos(d0.isoformat(), d1.isoformat())
    a0, a1 = _periodo_anterior(d0, d1)
    prev = _totales(_movimientos(a0.isoformat(), a1.isoformat()))
    kpi = _totales(movs)
    kpi["neto_usd"] = round(kpi["ingresos_usd"] - kpi["egresos_usd"], 2)
    kpi["movimientos"] = len(movs)
    kpi["anterior"] = {"desde": a0.isoformat(), "hasta": a1.isoformat(), "ingresos_usd": prev["ingresos_usd"], "egresos_usd": prev["egresos_usd"],
                       "neto_usd": round(prev["ingresos_usd"] - prev["egresos_usd"], 2)}
    with db.conectar() as con:
        kpi["por_cobrar_usd"] = round(con.execute("SELECT COALESCE(SUM(total), 0) FROM pedidos WHERE estado != 'cancelado' AND estado_pago = 'a_credito'").fetchone()[0] or 0, 2)
        kpi["por_pagar_usd"] = round(con.execute(
            "SELECT COALESCE(SUM(monto_usd), 0) FROM erp_compras WHERE forma_pago = 'pendiente' AND id NOT IN "
            "(SELECT compra_id FROM erp_bancos WHERE compra_id IS NOT NULL AND COALESCE(anulado, 0) = 0)").fetchone()[0] or 0, 2)
    por_dia = (d1 - d0).days <= 62
    clave = (lambda f: f) if por_dia else (lambda f: f[:7])
    cubo, cur = {}, d0
    if por_dia:
        while cur <= d1:
            cubo[cur.isoformat()] = [0.0, 0.0]
            cur += timedelta(days=1)
    else:
        y, m = d0.year, d0.month
        while (y, m) <= (d1.year, d1.month):
            cubo[f"{y:04d}-{m:02d}"] = [0.0, 0.0]
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    cats_e, cats_i, monedas = {}, {}, {"USD": {"ingresos": 0.0, "egresos": 0.0}, "VES": {"ingresos": 0.0, "egresos": 0.0}}
    for m in movs:
        if _es_capital(m["tipo"], m["categoria"]) or m["categoria"] in INTERNAS:
            continue
        if m["monto"] is not None and m["moneda"] in monedas:
            monedas[m["moneda"]]["ingresos" if m["tipo"] == "ingreso" else "egresos"] += m["monto"]
        if m["monto_usd"] is None:
            continue
        b = cubo.setdefault(clave(m["fecha"]), [0.0, 0.0])
        b[0 if m["tipo"] == "ingreso" else 1] += m["monto_usd"]
        dest = cats_i if m["tipo"] == "ingreso" else cats_e
        dest[m["categoria"]] = dest.get(m["categoria"], 0.0) + m["monto_usd"]
    ordenar = lambda dct, tipo: [{"categoria": k, "nombre": _nombre_categoria(tipo, k), "usd": round(v, 2)} for k, v in sorted(dct.items(), key=lambda kv: -kv[1])]
    return {"desde": d0.isoformat(), "hasta": d1.isoformat(), "agrupar": "dia" if por_dia else "mes", "kpi": kpi,
            "serie": [{"p": k, "ingresos": round(v[0], 2), "egresos": round(v[1], 2)} for k, v in sorted(cubo.items())],
            "egresos_categorias": ordenar(cats_e, "egreso"), "ingresos_fuentes": ordenar(cats_i, "ingreso"),
            "por_moneda": {k: {a: round(b, 2) for a, b in v.items()} for k, v in monedas.items()}}


def movimientos(desde=None, hasta=None, tipo=None, q=None, limite=300) -> dict:
    hoy = date.today()
    d1 = _fecha_flexible(hasta, hoy.isoformat())
    d0 = _fecha_flexible(desde, hoy.replace(day=1).isoformat())
    filas = _movimientos(d0, d1)
    if tipo in ("ingreso", "egreso"):
        filas = [m for m in filas if m["tipo"] == tipo]
    q = _plano(q)
    if q:
        filas = [m for m in filas if q in _plano(f"{m['concepto']} {m['cuenta']} {m['referencia']} {_nombre_categoria(m['tipo'], m['categoria'])}")]
    filas.sort(key=lambda m: (m["fecha"], m["id"]), reverse=True)
    for m in filas:
        m["categoria_nombre"] = _nombre_categoria(m["tipo"], m["categoria"])
        m["capital"] = _es_capital(m["tipo"], m["categoria"])
    return {"total": len(filas), "filas": filas[:max(1, min(int(limite or 300), 1000))]}
