"""
Migra los datos que hoy viven en CSV hacia crm.db (SQLite):
  - catalogo_50_modelos_venezuela.csv -> tabla catalogo
  - lista_espera.csv                  -> contactos + solicitudes_espera
  - clientes_mayoristas.csv           -> contactos (tipo='mayorista')

Se puede correr varias veces sin duplicar: si crm.db ya tiene catálogo
cargado, no lo vuelve a insertar (a menos que uses --forzar-catalogo).
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

CARPETA = os.path.dirname(__file__)


def migrar_catalogo(forzar=False):
    ruta = os.path.join(CARPETA, "catalogo_50_modelos_venezuela.csv")
    if not os.path.isfile(ruta):
        print("  (no encontré catalogo_50_modelos_venezuela.csv, salto este paso)")
        return

    with db.conectar() as con:
        ya_hay = con.execute("SELECT COUNT(*) AS n FROM catalogo").fetchone()["n"]
    if ya_hay and not forzar:
        print(f"  catalogo ya tiene {ya_hay} productos, no lo vuelvo a cargar (usa --forzar-catalogo para reemplazar)")
        return

    if forzar:
        with db.conectar() as con:
            con.execute("DELETE FROM catalogo")

    n = 0
    with open(ruta, newline="", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            db.insertar_producto(
                nombre=fila["nombre"],
                marca=fila["marca"],
                modelo=fila["modelo"],
                anio_desde=int(fila["anio_desde"]) if fila["anio_desde"] else None,
                anio_hasta=int(fila["anio_hasta"]) if fila["anio_hasta"] else None,
                precio=float(fila["precio"]),
                stock=int(fila["stock"]),
            )
            n += 1
    print(f"  catalogo: {n} productos migrados")


def migrar_lista_espera():
    ruta = os.path.join(CARPETA, "lista_espera.csv")
    if not os.path.isfile(ruta):
        print("  (no encontré lista_espera.csv, salto este paso)")
        return

    n = 0
    with open(ruta, newline="", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            telefono = fila.get("telefono") or f"sin-telefono-{fila.get('nombre_cliente', n)}"
            contacto_id = db.obtener_o_crear_contacto(telefono, tipo="cliente")
            db.actualizar_contacto(contacto_id, nombre=fila.get("nombre_cliente", ""))
            db.registrar_espera(contacto_id, fila.get("producto", ""))
            n += 1
    print(f"  lista_espera: {n} registros migrados")


def migrar_mayoristas():
    ruta = os.path.join(CARPETA, "clientes_mayoristas.csv")
    if not os.path.isfile(ruta):
        print("  (no encontré clientes_mayoristas.csv, salto este paso)")
        return

    n = 0
    with open(ruta, newline="", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            negocio = fila.get("negocio", "").strip()
            if not negocio or negocio.lower().startswith("ejemplo") or negocio.lower().startswith("prueba"):
                continue
            telefono = fila.get("telefono") or f"sin-telefono-{negocio}"
            estado = (fila.get("estado") or "nuevo").strip().lower()
            if estado == "pendiente":
                estado = "nuevo"  # el CSV viejo usaba "pendiente" como estado inicial
            contacto_id = db.obtener_o_crear_contacto(telefono, tipo="mayorista")
            db.actualizar_contacto(
                contacto_id,
                nombre=fila.get("contacto", ""),
                negocio=negocio,
                tipo_negocio=fila.get("tipo_negocio", ""),
                ciudad=fila.get("ciudad", ""),
                estado=estado,
                notas=fila.get("notas", ""),
            )
            n += 1
    print(f"  clientes_mayoristas: {n} registros migrados (se excluyen filas de ejemplo/prueba)")


if __name__ == "__main__":
    forzar = "--forzar-catalogo" in sys.argv
    print("Migrando datos a crm.db...")
    migrar_catalogo(forzar=forzar)
    migrar_lista_espera()
    migrar_mayoristas()
    print("Listo.")
