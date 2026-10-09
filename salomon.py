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

import agente as base
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
                        "modelo": {"type": "string", "description": "Modelo tal como lo devolvió el catálogo"},
                        "cantidad": {"type": "integer", "description": "Unidades, mínimo 1"},
                    },
                    "required": ["nombre", "marca", "modelo", "cantidad"],
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
                fila = con.execute(
                    "SELECT nombre, marca, modelo, precio, stock FROM catalogo "
                    "WHERE lower(nombre) = lower(?) AND lower(marca) = lower(?) AND lower(modelo) = lower(?)",
                    (linea.get("nombre", ""), linea.get("marca", ""), linea.get("modelo", "")),
                ).fetchone()
            if not fila:
                return {"error": f"Línea {i}: ese producto no existe en el catálogo. Búscalo con buscar_catalogo y usa sus datos exactos."}
            if fila["stock"] < cantidad:
                return {"error": f"Línea {i} ({fila['nombre']}): solo hay {fila['stock']} unidad(es) en stock."}
            if fila["precio"] <= 0:
                return {"error": f"Línea {i} ({fila['nombre']}): aún no tiene precio cargado. Pasa a una persona del equipo."}
            validadas.append((fila, cantidad))

        registradas, total_general = [], 0.0
        for fila, cantidad in validadas:
            producto = f'{fila["nombre"]} - {fila["marca"]} {fila["modelo"]}'
            with db.conectar() as con:
                repetido = con.execute(
                    "SELECT total FROM pedidos WHERE contacto_id = ? AND producto = ? AND cantidad = ? "
                    "AND estado = 'pendiente' AND fecha > datetime('now', '-10 minutes')",
                    (self.contacto_id, producto, cantidad),
                ).fetchone()
            if repetido:
                total = repetido["total"]
            else:
                total = db.registrar_pedido(self.contacto_id, producto, cantidad, fila["precio"])
            total_general += total
            self._precios.update({fila["precio"], total})
            registradas.append({
                "producto": producto, "cantidad": cantidad,
                "precio_unitario_usd": fila["precio"], "total_usd": total,
            })
            print(f"  🧾 [Salomón] pedido pendiente: {producto} x{cantidad} total=${total} ({self.telefono})")

        db.actualizar_contacto(self.contacto_id, estado="cliente")
        total_general = round(total_general, 2)
        self._precios.add(total_general)
        self._registro_hecho = True
        return {
            "mensaje": "Pedido registrado como PENDIENTE DE PAGO. La administración confirma el pago y luego se coordina el despacho.",
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
            HERRAMIENTA_PEDIDO, HERRAMIENTA_MIS_PEDIDOS, HERRAMIENTA_HUMANO,
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

            veredicto = vigilante.revisar_reglas(texto, self._precios, self._registro_hecho)
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
