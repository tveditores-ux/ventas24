"""
Agente de Importación.

Recibe los CSV de la liquidación de un embarque (estructura_costo, liquidacion_gastos,
liquidacion_parametros, pedido_items), los AUDITA y deja un borrador con su informe. No cambia
ningún costo ni existencia por su cuenta: una persona de administración lo aprueba y después
registra la recepción de la mercancía.

    analizar  -> borrador + informe (bloqueos / por revisar / información)
    aprobar   -> crea los productos que falten en el catálogo (sin precio ni existencia)
    recibir   -> entra la mercancía y fija el costo promedio ponderado:
                 nuevo = (existencia * costo actual + unidades * costo del embarque) / (existencia + unidades)
    descartar -> el borrador no sirve (se vuelve a subir uno corregido)

El costo del producto es el "costo unitario puesto en almacén" que calcula la liquidación; el IVA
de importación no entra si el parámetro iva_recuperable es SI.
"""

import csv
import io
import json
import zipfile

import db

ARCHIVOS = {
    "estructura_costo": ("sku", "unidades", "fob_neto_linea", "costo_importacion_asignado", "costo_unit_almacen", "orden_compra"),
    "liquidacion_gastos": ("concepto", "tipo_linea", "monto", "origen", "orden_compra"),
    "liquidacion_parametros": ("codigo", "valor", "fuente_nota"),
    "pedido_items": ("sku", "cantidad_confirmada", "verificacion", "marca"),
}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS erp_embarques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referencia TEXT NOT NULL,
    proveedor TEXT,
    estado TEXT NOT NULL DEFAULT 'borrador',    -- borrador / aprobado / recibido / descartado
    fob REAL, costo_total REAL, factor REAL,
    informe TEXT NOT NULL,
    creado_por INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
    aprobado_por INTEGER REFERENCES usuarios(id), aprobado_en TEXT,
    recibido_por INTEGER REFERENCES usuarios(id), recibido_en TEXT
);
CREATE TABLE IF NOT EXISTS erp_embarque_lineas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embarque_id INTEGER NOT NULL REFERENCES erp_embarques(id),
    sku TEXT NOT NULL, descripcion TEXT, marca TEXT,
    unidades INTEGER NOT NULL, fob_neto REAL, costo_unit_fob REAL, costo_unit_almacen REAL NOT NULL,
    catalogo_id INTEGER REFERENCES catalogo(id),
    recibido INTEGER NOT NULL DEFAULT 0
);
"""


def asegurar_tablas():
    with db.conectar() as con:
        con.executescript(ESQUEMA)
        if "tasa_aduana" not in {f["name"] for f in con.execute("PRAGMA table_info(erp_embarques)")}:
            con.execute("ALTER TABLE erp_embarques ADD COLUMN tasa_aduana REAL")   # Bs por USD del día de la declaración aduanera


def _num(x):
    try:
        return float(str(x).strip().replace(",", ".")) if str(x).strip() != "" else None
    except ValueError:
        return None


def _leer_csv(datos: bytes):
    for cod in ("utf-8-sig", "cp1252"):
        try:
            txt = datos.decode(cod)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(io.StringIO(txt)))


def _extraer(archivos: dict) -> dict:
    """archivos: {nombre: bytes}. Acepta los CSV sueltos o un ZIP (incluso con otro ZIP adentro)."""
    salida = {}

    def agregar(nombre, datos):
        base = nombre.replace("\\", "/").split("/")[-1].lower()
        if base.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(datos)) as z:
                if sum(i.file_size for i in z.infolist()) > 20_000_000:
                    raise ValueError("el ZIP es demasiado grande")
                for i in z.infolist():
                    if not i.is_dir():
                        agregar(i.filename, z.read(i))
        elif base.endswith(".csv"):
            salida[base[:-4]] = datos

    for nombre, datos in archivos.items():
        agregar(nombre, datos)
    return salida


def analizar(archivos: dict, usuario_id: int | None = None) -> dict:
    """Audita los CSV y guarda el borrador. Devuelve {id, informe}."""
    asegurar_tablas()
    crudos = _extraer(archivos)
    alertas = []

    def A(nivel, texto):
        alertas.append({"nivel": nivel, "texto": texto})

    faltan = [n for n in ARCHIVOS if n not in crudos]
    if faltan:
        raise ValueError("faltan archivos: " + ", ".join(f + ".csv" for f in faltan))
    t = {n: _leer_csv(crudos[n]) for n in ARCHIVOS}
    for n, cols in ARCHIVOS.items():
        if not t[n]:
            raise ValueError(f"{n}.csv está vacío")
        falta = [c for c in cols if c not in t[n][0]]
        if falta:
            raise ValueError(f"{n}.csv no tiene las columnas: {', '.join(falta)}")

    est, gastos, params, items = t["estructura_costo"], t["liquidacion_gastos"], t["liquidacion_parametros"], t["pedido_items"]
    refs = {r["orden_compra"] for r in est} | {r["orden_compra"] for r in gastos}
    if len(refs) != 1:
        raise ValueError(f"los archivos mezclan órdenes de compra distintas: {sorted(refs)}")
    ref = refs.pop()
    proveedor = (items[0].get("proveedor") or "").strip() or None
    prm = {p["codigo"]: p for p in params}

    # --- Cuadre de números --------------------------------------------------
    fob = sum(_num(r["fob_neto_linea"]) or 0 for r in est)
    asignado = sum(_num(r["costo_importacion_asignado"]) or 0 for r in est)
    total_liq = next((_num(g["monto"]) for g in gastos if g["concepto"].upper().startswith("COSTO TOTAL DE IMPORTA")), None)
    fob_param = _num((prm.get("valor_fob") or {}).get("valor"))
    if total_liq is None:
        A("bloquea", "La liquidación no trae la línea «COSTO TOTAL DE IMPORTACIÓN»: no puedo verificar el total.")
    elif abs(asignado - total_liq) > 1:
        A("bloquea", f"El costo asignado a los productos (${asignado:,.2f}) no cuadra con el costo total de la liquidación (${total_liq:,.2f}).")
    if fob_param is not None and abs(fob - fob_param) > 1:
        A("bloquea", f"El FOB de los productos (${fob:,.2f}) no cuadra con el FOB del pedido (${fob_param:,.2f}).")
    factor = round(asignado / fob, 4) if fob else None
    if factor and not 1.05 <= factor <= 1.8:
        A("revisar", f"Cada $1 de FOB está costando ${factor:.2f} puesto en almacén: es un factor poco usual, revisa los gastos.")

    conf = {r["sku"]: r for r in items}
    lineas = []
    for r in est:
        sku, u = r["sku"].strip(), int(_num(r["unidades"]) or 0)
        alm, asig = _num(r["costo_unit_almacen"]), _num(r["costo_importacion_asignado"])
        if not sku or u <= 0 or not alm or alm <= 0:
            A("bloquea", f"Línea {sku or '(sin código)'}: unidades o costo inválidos.")
            continue
        if asig and abs(asig / u - alm) > 0.02:
            A("bloquea", f"{sku}: el costo unitario ({alm}) no coincide con lo asignado entre las unidades ({asig / u:.4f}).")
        it = conf.get(sku)
        if not it:
            A("revisar", f"{sku}: no aparece en el detalle del pedido.")
        else:
            if (_num(it["cantidad_confirmada"]) or 0) != u:
                A("bloquea", f"{sku}: la estructura trae {u} unidades y el pedido confirmó {it['cantidad_confirmada']}.")
            if (it.get("verificacion") or "OK").strip().upper() != "OK":
                imp, cal = it.get("total_linea_oa_impreso"), it.get("total_linea_calculado")
                A("revisar", f"{sku}: el total impreso en la orden (${imp}) no coincide con el calculado (${cal}). "
                             "El costo usa el calculado; confirma cuál es el real con el proveedor.")
        lineas.append({"sku": sku, "descripcion": r.get("descripcion"), "marca": (it or {}).get("marca") or "",
                       "unidades": u, "fob_neto": _num(r["fob_neto_linea"]), "costo_unit_fob": _num(r.get("costo_unit_fob")),
                       "costo_unit_almacen": alm})
    if len({l["sku"].lower() for l in lineas}) != len(lineas):
        A("bloquea", "Hay códigos repetidos en la estructura de costo.")

    # --- Supuestos que todavía no son datos reales ------------------------
    nuevos = [g for g in gastos if (g.get("origen") or "").strip().upper() == "NUEVO" and g["tipo_linea"] == "detalle"]
    if nuevos:
        suma = sum(_num(g["monto"]) or 0 for g in nuevos)
        A("revisar", f"{len(nuevos)} gastos son estimados («NUEVO»), suman ${suma:,.2f} ({suma / asignado * 100:.1f} % del costo): "
                     "confírmalos con facturas reales antes de fijar precios.")
    for codigo, p in prm.items():
        nota = (p.get("fuente_nota") or "").lower()
        if any(w in nota for w in ("supuesto", "confirmar", "validar", "ajustar")):
            A("revisar", f"Parámetro «{p.get('parametro') or codigo}» = {p['valor']}: {p['fuente_nota']}")
    if (prm.get("iva_recuperable") or {}).get("valor", "").strip().upper() == "SI":
        A("info", "El IVA de importación NO está en el costo (se asume contribuyente ordinario con crédito fiscal). Lo confirma la contadora.")
    oper = [g for g in gastos if "operativ" in g["concepto"].lower() and g["tipo_linea"] == "detalle"]
    if any((_num(g["monto"]) or 0) == 0 for g in oper):
        A("revisar", "Los gastos operativos del embarque están en $0: faltan viáticos, papelería u otros.")

    # --- Enlace con el catálogo -------------------------------------------
    with db.conectar() as con:
        for l in lineas:
            filas = con.execute("SELECT id, marca FROM catalogo WHERE lower(codigo) = lower(?)", (l["sku"],)).fetchall()
            if len(filas) == 1:
                l["catalogo_id"] = filas[0]["id"]
            elif len(filas) > 1:
                m = [f for f in filas if f["marca"].lower() == l["marca"].lower()]
                l["catalogo_id"] = m[0]["id"] if len(m) == 1 else None
                if not l["catalogo_id"]:
                    A("bloquea", f"{l['sku']}: el código existe en varias marcas del catálogo y no puedo elegir cuál.")
            else:
                l["catalogo_id"] = None
        nuevos_cat = [l["sku"] for l in lineas if not l["catalogo_id"]]
        previo = con.execute("SELECT id FROM erp_embarques WHERE referencia = ? AND estado IN ('aprobado','recibido')", (ref,)).fetchone()
    if previo:
        A("bloquea", f"El embarque {ref} ya fue aprobado o recibido: no se puede cargar otra vez.")
    if nuevos_cat:
        A("info", f"{len(nuevos_cat)} productos no están en el catálogo y se crearán al aprobar (sin precio ni existencia): {', '.join(nuevos_cat[:20])}.")
    A("info", "El costo guardado es por UNIDAD. Falta definir si el catálogo vende por unidad o por caja de 12.")

    iva_imp = next((_num(g["monto"]) for g in gastos if g["concepto"].lower().startswith("iva de importaci") and g["tipo_linea"] == "detalle"), None)
    informe = {
        "iva_importacion_usd": iva_imp,
        "referencia": ref, "proveedor": proveedor, "fob": round(fob, 2), "costo_total": round(asignado, 2), "factor": factor,
        "unidades": sum(l["unidades"] for l in lineas),
        "costo_promedio_unidad": round(asignado / sum(l["unidades"] for l in lineas), 4) if lineas else None,
        "alertas": alertas,
        "bloqueos": sum(a["nivel"] == "bloquea" for a in alertas), "por_revisar": sum(a["nivel"] == "revisar" for a in alertas),
    }
    with db.conectar() as con:
        con.execute("UPDATE erp_embarques SET estado = 'descartado' WHERE referencia = ? AND estado = 'borrador'", (ref,))
        cur = con.execute(
            "INSERT INTO erp_embarques (referencia, proveedor, fob, costo_total, factor, informe, creado_por) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ref, proveedor, fob, asignado, factor, json.dumps(informe, ensure_ascii=False), usuario_id),
        )
        eid = cur.lastrowid
        con.executemany(
            "INSERT INTO erp_embarque_lineas (embarque_id, sku, descripcion, marca, unidades, fob_neto, costo_unit_fob, "
            "costo_unit_almacen, catalogo_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(eid, l["sku"], l["descripcion"], l["marca"], l["unidades"], l["fob_neto"], l["costo_unit_fob"],
              l["costo_unit_almacen"], l["catalogo_id"]) for l in lineas],
        )
    db.registrar_accion("Importación", None, "analizar_embarque", {"referencia": ref}, {k: informe[k] for k in ("bloqueos", "por_revisar", "costo_total")})
    return {"id": eid, "informe": informe}


def obtener(embarque_id: int) -> dict | None:
    asegurar_tablas()
    with db.conectar() as con:
        e = con.execute("SELECT * FROM erp_embarques WHERE id = ?", (embarque_id,)).fetchone()
        if not e:
            return None
        ls = con.execute("SELECT * FROM erp_embarque_lineas WHERE embarque_id = ? ORDER BY id", (embarque_id,)).fetchall()
    d = dict(e)
    d["informe"] = json.loads(d["informe"])
    iva = d["informe"].get("iva_importacion_usd")
    d["iva_credito_bs"] = round(iva * d["tasa_aduana"], 2) if iva and d.get("tasa_aduana") else None
    d["lineas"] = [dict(l) for l in ls]
    return d


def listar() -> list:
    asegurar_tablas()
    with db.conectar() as con:
        filas = con.execute(
            "SELECT id, referencia, proveedor, estado, fob, costo_total, factor, creado_en, recibido_en FROM erp_embarques "
            "WHERE estado != 'descartado' ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [dict(f) for f in filas]


def aprobar(embarque_id: int, usuario_id: int, acepto_revisar: bool = False) -> dict:
    e = obtener(embarque_id)
    if not e:
        raise ValueError("embarque no encontrado")
    if e["estado"] != "borrador":
        raise ValueError(f"el embarque está '{e['estado']}'")
    inf = e["informe"]
    if inf["bloqueos"]:
        raise ValueError(f"tiene {inf['bloqueos']} bloqueo(s): corrige los archivos y vuelve a subirlos")
    if inf["por_revisar"] and not acepto_revisar:
        raise ValueError(f"tiene {inf['por_revisar']} punto(s) por revisar: confirma que los revisaste para aprobar")
    creados = 0
    with db.conectar() as con:
        for l in e["lineas"]:
            if l["catalogo_id"]:
                continue
            cur = con.execute(
                "INSERT INTO catalogo (nombre, marca, modelo, precio, stock, codigo, stock_verificado, costo) VALUES (?, ?, '', 0, 0, ?, 0, NULL)",
                (l["descripcion"] or l["sku"], l["marca"] or "SIN MARCA", l["sku"]),
            )
            con.execute("UPDATE erp_embarque_lineas SET catalogo_id = ? WHERE id = ?", (cur.lastrowid, l["id"]))
            creados += 1
        con.execute("UPDATE erp_embarques SET estado = 'aprobado', aprobado_por = ?, aprobado_en = CURRENT_TIMESTAMP WHERE id = ?",
                    (usuario_id, embarque_id))
    db.registrar_accion("Importación", None, "aprobar_embarque", {"id": embarque_id, "aceptó_revisar": acepto_revisar}, {"productos_creados": creados})
    return {"productos_creados": creados}


def recibir(embarque_id: int, usuario_id: int, recibidos: dict | None = None) -> dict:
    """Entra la mercancía y fija el costo promedio ponderado. `recibidos` = {sku: unidades} si llegó distinto a lo pedido."""
    e = obtener(embarque_id)
    if not e:
        raise ValueError("embarque no encontrado")
    if e["estado"] != "aprobado":
        raise ValueError(f"el embarque está '{e['estado']}': solo se recibe uno aprobado")
    resultado, repuestos = [], []
    with db.conectar() as con:
        for l in e["lineas"]:
            cant = int((recibidos or {}).get(l["sku"], l["unidades"]))
            if cant < 0:
                raise ValueError("no se puede recibir una cantidad negativa")
            if cant == 0:
                continue
            p = con.execute("SELECT * FROM catalogo WHERE id = ?", (l["catalogo_id"],)).fetchone()
            antes = p["stock"] if p["stock_verificado"] else 0
            costo_ant = p["costo"]
            if antes > 0 and costo_ant:
                nuevo = (antes * costo_ant + cant * l["costo_unit_almacen"]) / (antes + cant)
            else:
                nuevo = l["costo_unit_almacen"]
            nuevo = round(nuevo, 4)
            con.execute("UPDATE catalogo SET stock = ?, stock_verificado = 1, stock_prueba = 0, costo = ? WHERE id = ?",
                        (antes + cant, nuevo, l["catalogo_id"]))
            con.execute("UPDATE erp_embarque_lineas SET recibido = ? WHERE id = ?", (cant, l["id"]))
            db.registrar_movimiento(con, l["catalogo_id"], "recepcion", cant, referencia=f"embarque {e['referencia']}",
                                    usuario_id=usuario_id, costo=l["costo_unit_almacen"])
            if antes <= 0:
                repuestos.append({"catalogo_id": l["catalogo_id"], "codigo": p["codigo"], "nombre": p["nombre"]})
            resultado.append({"sku": l["sku"], "recibidas": cant, "existencia_antes": antes, "costo_antes": costo_ant,
                              "costo_embarque": l["costo_unit_almacen"], "costo_promedio": nuevo})
        con.execute("UPDATE erp_embarques SET estado = 'recibido', recibido_por = ?, recibido_en = CURRENT_TIMESTAMP WHERE id = ?",
                    (usuario_id, embarque_id))
        for r in repuestos:
            db.emitir_evento(con, "stock.repuesto", r)
    db.registrar_accion("Importación", None, "recibir_embarque", {"id": embarque_id}, {"lineas": len(resultado)})
    return {"lineas": resultado}


def fijar_tasa_aduana(embarque_id: int, tasa: float):
    """El IVA de importación es crédito fiscal a la tasa del día de la declaración aduanera: no se reexpresa después."""
    if not tasa or tasa <= 0:
        raise ValueError("la tasa debe ser mayor que cero")
    with db.conectar() as con:
        cur = con.execute("UPDATE erp_embarques SET tasa_aduana = ? WHERE id = ? AND estado != 'descartado'", (round(tasa, 4), embarque_id))
    if cur.rowcount == 0:
        raise ValueError("embarque no encontrado")
    db.registrar_accion("Importación", None, "tasa_aduana", {"id": embarque_id, "tasa": tasa}, "ok")


def descartar(embarque_id: int):
    with db.conectar() as con:
        cur = con.execute("UPDATE erp_embarques SET estado = 'descartado' WHERE id = ? AND estado = 'borrador'", (embarque_id,))
    if cur.rowcount == 0:
        raise ValueError("solo se descarta un borrador")
