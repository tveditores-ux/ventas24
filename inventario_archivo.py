"""
Carga de inventario desde PDF o imagen (PNG/JPG). Claude lee el documento y devuelve las filas;
nada se aplica hasta que una persona revisa la vista previa y confirma. Un error de lectura
no debe mover existencias, por eso este módulo solo extrae y compara con el catálogo.
"""
import base64
import json
import os

from anthropic import Anthropic

import agente as base
import db

MAX_BYTES = 8 * 1024 * 1024
TIPOS_IMAGEN = {"image/png": "image/png", "image/jpeg": "image/jpeg", "image/jpg": "image/jpeg"}
MAX_FILAS = 1500

HERRAMIENTA = {
    "name": "registrar_filas",
    "description": "Registra las filas de inventario leídas del documento.",
    "input_schema": {
        "type": "object",
        "properties": {
            "filas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "codigo": {"type": "string", "description": "código del producto tal como aparece"},
                        "cantidad": {"type": "number", "description": "existencia contada"},
                        "marca": {"type": "string"},
                        "costo": {"type": "number"},
                        "minimo": {"type": "number"},
                    },
                    "required": ["codigo", "cantidad"],
                },
            },
            "ilegibles": {"type": "integer", "description": "renglones que no se pudieron leer con seguridad"},
        },
        "required": ["filas"],
    },
}

INSTRUCCION = (
    "Lee este documento de inventario y devuelve una fila por producto con su código y la cantidad en existencia. "
    "Agrega marca, costo y mínimo solo si el documento los trae. No inventes ni completes datos: si un renglón no "
    "se lee con seguridad, no lo incluyas y súmalo a 'ilegibles'. Copia los códigos exactamente como están escritos."
)


def tipo_de(nombre: str, content_type: str | None) -> str | None:
    n = (nombre or "").lower()
    if n.endswith(".pdf") or content_type == "application/pdf":
        return "pdf"
    if n.endswith((".png", ".jpg", ".jpeg")) or (content_type or "") in TIPOS_IMAGEN:
        return "imagen"
    return None


def _bloque(tipo: str, datos: bytes, nombre: str, content_type: str | None) -> dict:
    b64 = base64.standard_b64encode(datos).decode()
    if tipo == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    medio = TIPOS_IMAGEN.get(content_type or "") or ("image/png" if nombre.lower().endswith(".png") else "image/jpeg")
    return {"type": "image", "source": {"type": "base64", "media_type": medio, "data": b64}}


def extraer(nombre: str, content_type: str | None, datos: bytes) -> dict:
    tipo = tipo_de(nombre, content_type)
    if not tipo:
        raise ValueError("formato no admitido: usa PDF, CSV, PNG o JPG")
    if not datos:
        raise ValueError("el archivo está vacío")
    if len(datos) > MAX_BYTES:
        raise ValueError("el archivo pesa más de 8 MB")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("falta ANTHROPIC_API_KEY para leer PDF e imágenes; el CSV sigue funcionando")
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    cliente = Anthropic(api_key=api_key, default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None)
    try:
        r = cliente.messages.create(
            model=base.MODELO, max_tokens=8192, tools=[HERRAMIENTA],
            tool_choice={"type": "tool", "name": "registrar_filas"},
            messages=[{"role": "user", "content": [_bloque(tipo, datos, nombre, content_type),
                                                   {"type": "text", "text": INSTRUCCION}]}],
        )
    except Exception as e:  # el detalle va al log del servidor, no a la pantalla
        print(f"[inventario_archivo] error leyendo {nombre}: {e}", flush=True)
        raise ValueError("no pude leer el archivo; prueba con una foto más nítida o súbelo como CSV")
    uso = next((b for b in r.content if b.type == "tool_use"), None)
    if not uso:
        raise ValueError("no encontré filas de inventario en el archivo")
    filas = (uso.input or {}).get("filas") or []
    if not filas:
        raise ValueError("no encontré filas de inventario en el archivo")
    return {"filas": filas[:MAX_FILAS], "ilegibles": int((uso.input or {}).get("ilegibles") or 0)}


def vista_previa(filas: list) -> dict:
    """Compara cada fila con el catálogo: producto, existencia actual y estado (ok / no_encontrado / ambiguo / invalida)."""
    salida = []
    with db.conectar() as con:
        for f in filas:
            codigo = str(f.get("codigo") or "").strip()
            marca = str(f.get("marca") or "").strip()
            fila = {"codigo": codigo, "marca": marca, "cantidad": f.get("cantidad"),
                    "costo": f.get("costo"), "minimo": f.get("minimo")}
            try:
                fila["cantidad"] = int(float(str(f.get("cantidad")).replace(",", ".")))
                if fila["cantidad"] < 0 or not codigo:
                    raise ValueError
            except (ValueError, TypeError):
                fila["estado"] = "invalida"
                salida.append(fila)
                continue
            if marca:
                ps = con.execute("SELECT * FROM catalogo WHERE lower(codigo)=lower(?) AND lower(marca)=lower(?)", (codigo, marca)).fetchall()
            else:
                ps = con.execute("SELECT * FROM catalogo WHERE lower(codigo)=lower(?)", (codigo,)).fetchall()
            if not ps:
                fila["estado"] = "no_encontrado"
            elif len(ps) > 1:
                fila["estado"] = "ambiguo"
            else:
                p = ps[0]
                fila.update(estado="ok", nombre=p["nombre"], marca=p["marca"] or marca,
                            actual=p["stock"] if p["stock_verificado"] else None)
            salida.append(fila)
    cuenta = {}
    for f in salida:
        cuenta[f["estado"]] = cuenta.get(f["estado"], 0) + 1
    return {"filas": salida, "resumen": cuenta}
