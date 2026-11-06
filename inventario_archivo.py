"""
Carga de inventario desde PDF o imagen (PNG/JPG). Todo se lee dentro de Ventas-24: el texto del PDF con
pypdf y las fotos con OCR local (Tesseract). Ningún archivo se envía a servicios externos.
Nada se aplica hasta que una persona revisa la vista previa y confirma; un error de lectura no debe
mover existencias, por eso este módulo solo extrae y compara con el catálogo.
"""
import io
import os
import re
import shutil
import subprocess
import tempfile

import db

MAX_BYTES = 8 * 1024 * 1024
TIPOS_IMAGEN = {"image/png", "image/jpeg", "image/jpg"}
MAX_FILAS = 1500
_NUM = re.compile(r"^\d{1,6}(?:[.,]\d+)?$")


def tipo_de(nombre: str, content_type: str | None) -> str | None:
    n = (nombre or "").lower()
    if n.endswith(".pdf") or content_type == "application/pdf":
        return "pdf"
    if n.endswith((".png", ".jpg", ".jpeg")) or (content_type or "") in TIPOS_IMAGEN:
        return "imagen"
    return None


def _texto_pdf(datos: bytes) -> str:
    from pypdf import PdfReader
    try:
        lector = PdfReader(io.BytesIO(datos))
        return "\n".join((pg.extract_text() or "") for pg in lector.pages[:60])
    except Exception as e:
        print(f"[inventario_archivo] pdf ilegible: {e}", flush=True)
        raise ValueError("no pude abrir el PDF")


def _texto_imagen(datos: bytes) -> str:
    """OCR local con Tesseract: la imagen no sale del servidor de Ventas-24."""
    if not shutil.which("tesseract"):
        raise ValueError("el servidor aún no tiene el lector de imágenes (Tesseract) instalado; usa PDF o CSV mientras tanto")
    with tempfile.TemporaryDirectory() as d:
        ruta = os.path.join(d, "foto")
        with open(ruta, "wb") as f:
            f.write(datos)
        for idioma in ("spa+eng", "eng"):
            r = subprocess.run(["tesseract", ruta, "stdout", "-l", idioma, "--psm", "6"], capture_output=True, timeout=90)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", errors="replace")
    raise ValueError("no pude leer la imagen; prueba con una foto más nítida y derecha")


def _entero(tok: str):
    if not _NUM.match(tok):
        return None
    v = float(tok.replace(",", "."))
    return int(v) if v == int(v) else None


def interpretar(texto: str) -> dict:
    """Busca en cada renglón un código del catálogo y la primera cantidad entera que le sigue.
    Lo que no coincide con el catálogo no se adivina: se cuenta como ilegible."""
    with db.conectar() as con:
        filas_cat = con.execute("SELECT codigo, marca FROM catalogo WHERE codigo IS NOT NULL AND codigo != ''").fetchall()
    codigos, marcas = set(), set()
    for c in filas_cat:
        codigos.add(c["codigo"].strip().lower())
        if c["marca"]:
            marcas.add(c["marca"].strip())
    filas, ilegibles, vistos = [], 0, set()
    for linea in texto.splitlines():
        toks = [t for t in re.split(r"[\s;|]+", linea.strip()) if t]
        if not toks:
            continue
        pos = next((k for k, t in enumerate(toks) if t.strip(".,:()").lower() in codigos), None)
        if pos is None:
            if any(_NUM.match(t) for t in toks) and any(re.search(r"[A-Za-z]", t) and re.search(r"\d", t) for t in toks):
                ilegibles += 1
            continue
        codigo = toks[pos].strip(".,:()")
        cant = next((v for v in (_entero(t) for t in toks[pos + 1:]) if v is not None), None)
        if cant is None:
            ilegibles += 1
            continue
        marca = next((m for m in marcas if m.lower() in linea.lower()), "")
        clave = (codigo.lower(), marca.lower(), cant)
        if clave in vistos:
            continue
        vistos.add(clave)
        fila = {"codigo": codigo, "cantidad": cant}
        if marca:
            fila["marca"] = marca
        filas.append(fila)
    return {"filas": filas[:MAX_FILAS], "ilegibles": ilegibles}


def extraer(nombre: str, content_type: str | None, datos: bytes) -> dict:
    tipo = tipo_de(nombre, content_type)
    if not tipo:
        raise ValueError("formato no admitido: usa PDF, CSV, PNG o JPG")
    if not datos:
        raise ValueError("el archivo está vacío")
    if len(datos) > MAX_BYTES:
        raise ValueError("el archivo pesa más de 8 MB")
    texto = _texto_pdf(datos) if tipo == "pdf" else _texto_imagen(datos)
    if len(texto.strip()) < 10:
        raise ValueError("no encontré texto en el archivo (¿PDF escaneado o foto borrosa?); prueba con una foto nítida o el CSV")
    r = interpretar(texto)
    if not r["filas"]:
        raise ValueError("no reconocí ningún código del catálogo en el archivo")
    return r


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
