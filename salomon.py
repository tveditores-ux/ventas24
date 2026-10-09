"""
Salomón: el vendedor de élite (plan Premium) de ventas24, para mayoristas.

- Cerebro: salomon_cerebro.md (valores, personalidad, reglas). Se edita a mano
  y se versiona en git; el agente no puede reescribirlo.
- Vigilante: salomon_vigilante.py revisa cada respuesta antes de enviarla.
- Herramientas: catálogo y lista de espera (las de siempre), más
  registrar_pedido (varias líneas; el PRECIO sale de la base, no del modelo),
  ver_mis_pedidos (reposición) y pasar_a_humano.

Hereda de Agente, así que el resto del sistema (webhook, CRM, monitor) lo usa
igual que al vendedor anterior. Se activa con la variable VENDEDOR (ver
perfiles.py). Los pedidos quedan 'por_pagar': solo la administración confirma
el pago y solo entonces se despacha.
"""

import json
import os
import uuid

import agente as base
import ciclo
import db
import eventos
import perfiles
import salomon_vigilante as vigilante

MAX_VUELTAS = 8

MENSAJE_DERIVACION = (
    "Dame un momento, voy a pasar tu consulta a una persona del equipo "
    "para que te responda con exactitud."
)

HERRAMIENTA_PEDIDO = {
    "name": "registrar_pedido",
    "description": (
        "Registra un pedido que el cliente ya confirmó, con una o varias líneas. Cada línea "
        "lleva el producto EXACTAMENTE como lo devolvió buscar_catalogo (nombre, marca y "
        "modelo). El precio lo pone el sistema. El pedido queda pendiente de pago hasta que "
        "la administración lo confirme."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lineas": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "nombre": {"type": "string", "description": "Nombre del producto tal como lo devolvió el catálogo"},
                        "marca": {"type": "string", "description": "Marca tal como la devolvió el catálogo"},
                        "modelo": {"type": "string", "description": "Modelo tal como lo devolvió el catálogo (vacío si no trae)"},
                        "codigo": {"type": "string", "description": "Código del producto, si el catálogo lo devolvió (preferido para identificarlo)"},
                        "cantidad": {"type": "integer", "description": "Unidades, mínimo 1"},
                    },
                    "required": ["nombre", "marca", "cantidad"],
                },
            },
        },
        "required": ["lineas"],
    },
}

HERRAMIENTA_MIS_PEDIDOS = {
    "name": "ver_mis_pedidos",
    "description": (
        "Devuelve los últimos pedidos de este cliente (producto, cantidad, total, estado). "
        "Úsala para proponer reposición o repetir una compra anterior."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

HERRAMIENTA_EQUIPO = {
    "name": "solicitar_al_equipo",
    "description": (
        "Deja una solicitud para una persona del equipo SIN detener la conversación: la persona "
        "la ve en el CRM y responde en este mismo chat. Úsala para condiciones por volumen, "
        "descuentos, crédito, o cualquier cosa que no puedas resolver tú pero que no sea un "
        "reclamo (para reclamos o clientes molestos usa pasar_a_humano)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tipo": {"type": "string", "enum": ["condiciones", "existencia", "pago", "otro"]},
            "asunto": {"type": "string", "description": "Qué se necesita, en una frase"},
            "detalle": {"type": "string", "description": "Lo que el equipo debe saber: producto, cantidades, tipo de negocio, lo que pidió el cliente"},
        },
        "required": ["tipo", "asunto", "detalle"],
    },
}

HERRAMIENTA_HUMANO = {
    "name": "pasar_a_humano",
    "description": (
        "Pasa la conversación a una persona del equipo y detiene las respuestas "
        "automáticas. Úsala en reclamos, clientes molestos, descuentos, crédito o dudas que "
        "no puedas resolver con certeza."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"motivo": {"type": "string", "description": "Por qué pasa a una persona, en una frase"}},
        "required": ["motivo"],
    },
}


def _construir_prompt(perfil) -> str:
    with open(perfil.cerebro, encoding="utf-8") as f:
        return f.read()


def _bitacora(contacto_id, capa, motivo, borrador, accion):
    """Deja constancia de cada objeción del vigilante. Tabla propia, creada si no existe."""
    with db.conectar() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS salomon_bitacora ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, contacto_id INTEGER, "
            "fecha TEXT DEFAULT CURRENT_TIMESTAMP, capa TEXT, motivo TEXT, "
            "borrador TEXT, accion TEXT)"
        )
        con.execute(
            "INSERT INTO salomon_bitacora (contacto_id, capa, motivo, borrador, accion) VALUES (?, ?, ?, ?, ?)",
            (contacto_id, capa, motivo, borrador, accion),
        )


class Salomon(base.Agente):
    def __init__(self, api_key=None, telefono=None, tipo="mayorista", perfil=None):
        super().__init__(api_key=api_key, telefono=telefono, tipo=tipo)
        self.perfil = perfil or perfiles.SALOMON
        self.modelo = self.perfil.modelo or base.MODELO
        self.modelo_vigilante = self.perfil.modelo_vigilante or os.environ.get("VIGILANTE_MODELO") or base.MODELO
        self.system_prompt = _construir_prompt(self.perfil)
        self.tipo_precio = "mayorista"   # Salomón siempre cotiza con la lista de mayor
        self.mensajes_posteriores = []   # avisos del sistema que salen justo después de su respuesta
        self._precios = set()        # precios y totales vistos en este turno
        self._resultados = []        # resultados de herramientas de este turno (para el revisor)
        self._registro_hecho = False

    # -- herramientas ---------------------------------------------------
    def _ejecutar_herramienta(self, nombre, params):
        if nombre == "buscar_catalogo":
            resultado = super()._ejecutar_herramienta(nombre, params)
            if isinstance(resultado, list):
                self._precios.update(p["precio_usd"] for p in resultado)
            return resultado

        if nombre == "registrar_pedido":
            return self._registrar_pedido(params)

        if nombre == "ver_mis_pedidos":
            pedidos = db.listar_pedidos(contacto_id=self.contacto_id)[:10]
            for p in pedidos:
                self._precios.update({p["precio_unitario"], p["total"]})
            return [
                {"producto": p["producto"], "cantidad": p["cantidad"], "precio_unitario_usd": p["precio_unitario"],
                 "total_usd": p["total"], "fecha": (p["fecha"] or "")[:10], "estado_pago": p["estado_pago"]}
                for p in pedidos
            ] or {"mensaje": "Este cliente todavía no tiene pedidos"}

        if nombre == "solicitar_al_equipo":
            solicitud = db.crear_solicitud(
                self.contacto_id, params.get("tipo", "otro"), params.get("asunto", "Solicitud"), params.get("detalle")
            )
            eventos.registrar("solicitud_equipo", self.telefono, params.get("asunto", "")[:80], agente_tipo=self.tipo)
            return {"mensaje": "Solicitud registrada. Una persona del equipo la verá en el CRM y le escribirá al cliente "
                               "en este mismo chat. No prometas tiempos.", "solicitud": solicitud}

        if nombre == "pasar_a_humano":
            self._derivar(params.get("motivo", "sin motivo"))
            return {"mensaje": "Conversación pasada a una persona del equipo"}

        return super()._ejecutar_herramienta(nombre, params)

    def _registrar_pedido(self, params):
        lineas = params.get("lineas")
        if not isinstance(lineas, list) or not lineas:
            return {"error": "Falta al menos una línea de pedido."}

        # Se valida TODO antes de guardar nada: un pedido a medias es peor que ninguno.
        validadas = []
        for i, linea in enumerate(lineas, 1):
            cantidad = linea.get("cantidad", 0)
            if not isinstance(cantidad, int) or isinstance(cantidad, bool) or cantidad < 1:
                return {"error": f"Línea {i}: la cantidad debe ser un entero de al menos 1."}
            with db.conectar() as con:
                fila = None
                if linea.get("codigo"):
                    fila = con.execute(
                        "SELECT * FROM catalogo WHERE lower(codigo) = lower(?) AND lower(marca) = lower(?)",
                        (linea["codigo"], linea.get("marca", "")),
                    ).fetchone()
                if not fila:
                    fila = con.execute(
                        "SELECT * FROM catalogo WHERE lower(nombre) = lower(?) AND lower(marca) = lower(?) "
                        "AND lower(modelo) = lower(?)",
                        (linea.get("nombre", ""), linea.get("marca", ""), linea.get("modelo") or ""),
                    ).fetchone()
            if not fila:
                return {"error": f"Línea {i}: ese producto no existe en el catálogo. Búscalo con buscar_catalogo y usa sus datos exactos."}
            if fila["stock_verificado"] and fila["stock"] < cantidad:
                return {"error": f"Línea {i} ({fila['nombre']}): solo hay {fila['stock']} unidad(es) en stock."}
            precio = db.precio_para(fila, self.tipo_precio)
            if not precio or precio <= 0:
                return {"error": f"Línea {i} ({fila['nombre']}): aún no tiene precio cargado. Pasa a una persona del equipo."}
            validadas.append((fila, cantidad, precio))

        # Salomón tiene acceso al inventario: si TODAS las líneas tienen existencia confirmada y alcanzan,
        # confirma él mismo la existencia y reserva el stock. Si alguna no tiene inventario cargado,
        # la orden queda para que una persona del equipo la verifique.
        auto = all(f["stock_verificado"] and f["stock"] >= c for f, c, _ in validadas)
        estado_pago = "por_pagar" if auto else "por_confirmar"
        productos = []
        for fila, cantidad, precio in validadas:
            if fila["codigo"]:
                producto = f'[{fila["codigo"]}] {fila["nombre"][:80]} ({fila["marca"]})'
            else:
                producto = f'{fila["nombre"]} - {fila["marca"]} {fila["modelo"]}'.strip()
            productos.append(producto)

        # Evita duplicar si el mismo pedido ya se registró hace instantes (reintento del modelo).
        with db.conectar() as con:
            repetido = all(
                con.execute(
                    "SELECT 1 FROM pedidos WHERE contacto_id = ? AND producto = ? AND cantidad = ? "
                    "AND estado != 'cancelado' AND fecha > datetime('now', '-10 minutes')",
                    (self.contacto_id, productos[i], validadas[i][1]),
                ).fetchone()
                for i in range(len(validadas))
            )
        orden = uuid.uuid4().hex[:10]
        lineas_bd = [
            {"producto": productos[i], "cantidad": c, "precio": p, "catalogo_id": f["id"], "reservar": auto}
            for i, (f, c, p) in enumerate(validadas)
        ]
        if repetido:
            totales = [round(c * p, 2) for _, c, p in validadas]
        else:
            try:
                totales = db.crear_orden(self.contacto_id, orden, lineas_bd, estado_pago, "Salomón" if auto else None)
            except ValueError as e:
                return {"error": f"No se pudo reservar: {e}. Vuelve a consultar el catálogo."}
            db.actualizar_contacto(self.contacto_id, estado="cliente")
            if auto:
                # El total y los datos de pago los manda el sistema justo después de la respuesta de Salomón.
                with db.conectar() as con:
                    primera = con.execute("SELECT id FROM pedidos WHERE orden = ? ORDER BY id LIMIT 1", (orden,)).fetchone()["id"]
                self.mensajes_posteriores.append(ciclo.texto_existencia_confirmada(primera))
                eventos.registrar("existencia", self.telefono, f"confirmada por Salomón (orden {orden})", agente_tipo=self.tipo)

        registradas, total_general = [], 0.0
        for i, (fila, cantidad, precio) in enumerate(validadas):
            total_general += totales[i]
            self._precios.update({precio, totales[i]})
            registradas.append({
                "producto": productos[i], "cantidad": cantidad,
                "precio_unitario_usd": precio, "total_usd": totales[i],
            })
            print(f"  🧾 [Salomón] pedido {'con existencia confirmada' if auto else 'por confirmar'}: {productos[i]} x{cantidad} total=${totales[i]} ({self.telefono})")

        total_general = round(total_general, 2)
        self._precios.add(total_general)
        self._registro_hecho = True
        if auto:
            mensaje = (
                "Pedido registrado y EXISTENCIA CONFIRMADA por ti con el inventario; el stock quedó reservado. "
                "El sistema le envía al cliente, justo después de tu mensaje, el total final y los datos de pago. "
                "No los escribas tú: dile que le llegan en el siguiente mensaje y que luego envíe el comprobante por este chat; "
                "la administración valida el pago y después se despacha."
            )
        else:
            mensaje = (
                "Pedido registrado. Alguna línea no tiene inventario cargado, así que una persona del equipo verifica la "
                "existencia antes de cobrar; cuando lo haga, el cliente recibe por este chat el total y los datos de pago."
            )
        return {
            "mensaje": mensaje,
            "existencia": "confirmada y reservada" if auto else "por confirmar con el equipo",
            "lineas": registradas,
            "total_general_usd": total_general,
        }

    def _derivar(self, motivo):
        with db.conectar() as con:
            fila = con.execute("SELECT notas FROM contactos WHERE id = ?", (self.contacto_id,)).fetchone()
        notas = ((fila["notas"] if fila and fila["notas"] else "") + f"\n[Salomón → humano] {motivo}").strip()
        db.actualizar_contacto(self.contacto_id, modo_manual=1, notas=notas)
        eventos.registrar("modo_manual", self.telefono, f"Salomón derivó: {motivo}", agente_tipo=self.tipo)

    # -- conversación ---------------------------------------------------
    def procesar_mensaje(self, texto_usuario: str, historial_previo=None) -> str:
        db.tocar_ultimo_contacto(self.contacto_id)

        historial = historial_previo if historial_previo is not None else db.obtener_historial(self.contacto_id)
        historial = list(historial)
        historial.append({"role": "user", "content": texto_usuario})
        historial = base._fusionar_consecutivos(historial)

        self._precios, self._resultados, self._registro_hecho = set(), [], False
        for msg in historial:
            if msg["role"] == "assistant" and isinstance(msg["content"], str):
                self._precios |= vigilante.montos_en(msg["content"])

        herramientas = [
            base.HERRAMIENTA_CATALOGO, base.HERRAMIENTA_LISTA_ESPERA,
            HERRAMIENTA_PEDIDO, HERRAMIENTA_MIS_PEDIDOS, HERRAMIENTA_EQUIPO, HERRAMIENTA_HUMANO,
        ]
        reintentos = 0

        for _ in range(MAX_VUELTAS):
            eventos.registrar("claude_llamado", self.telefono, self.modelo, agente_tipo=self.tipo)
            respuesta = self.client.messages.create(
                model=self.modelo,
                max_tokens=1200,
                system=self.system_prompt,
                tools=herramientas,
                messages=historial,
            )
            historial.append({"role": "assistant", "content": respuesta.content})
            bloques = [b for b in respuesta.content if b.type == "tool_use"]

            if bloques:
                resultados = []
                for b in bloques:
                    resultado = self._ejecutar_herramienta(b.name, b.input)
                    self._resultados.append({"herramienta": b.name, "resultado": resultado})
                    eventos.registrar("herramienta", self.telefono, b.name, agente_tipo=self.tipo)
                    resultados.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })
                historial.append({"role": "user", "content": resultados})
                continue

            texto = "".join(b.text for b in respuesta.content if b.type == "text").strip()

            # Si Salomón pasó la conversación a una persona, no se vigila ni se reintenta.
            if any(r["herramienta"] == "pasar_a_humano" for r in self._resultados):
                return self._guardar(texto_usuario, texto or MENSAJE_DERIVACION)

            equipo_avisado = any(r["herramienta"] == "solicitar_al_equipo" for r in self._resultados)
            veredicto = vigilante.revisar_reglas(texto, self._precios, self._registro_hecho, equipo_avisado)
            if veredicto.ok:
                veredicto = vigilante.revisar_con_modelo(
                    self.client, self.modelo_vigilante, texto, historial, self._resultados
                )
            if veredicto.ok:
                return self._guardar(texto_usuario, texto)

            reintentos += 1
            eventos.registrar("vigilante", self.telefono, f"objeción ({veredicto.capa}): {veredicto.motivo}", ok=False, agente_tipo=self.tipo)
            if reintentos > self.perfil.max_reintentos:
                _bitacora(self.contacto_id, veredicto.capa, veredicto.motivo, texto, "derivado a humano")
                self._derivar("el vigilante rechazó la respuesta varias veces: " + veredicto.motivo)
                return self._guardar(texto_usuario, MENSAJE_DERIVACION)

            _bitacora(self.contacto_id, veredicto.capa, veredicto.motivo, texto, "reintento")
            historial.append({
                "role": "user",
                "content": "[REVISIÓN INTERNA — el cliente no ve esto] Tu respuesta no puede enviarse: "
                           f"{veredicto.motivo} Escribe de nuevo solo el mensaje para el cliente, corregido.",
            })

        # Demasiadas vueltas de herramientas sin una respuesta final: mejor una persona.
        self._derivar("Salomón no llegó a una respuesta final")
        return self._guardar(texto_usuario, MENSAJE_DERIVACION)

    def _guardar(self, texto_usuario, texto):
        db.registrar_interaccion(self.contacto_id, "user", texto_usuario)
        db.registrar_interaccion(self.contacto_id, "assistant", texto)
        return texto
