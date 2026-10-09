"""
Vigilante de Salomón: revisa cada respuesta antes de que llegue al cliente.

Dos capas:
  1. Reglas fijas (rápidas, sin costo): precios inventados, negar ser IA,
     prometer lo que no consta, decir "pedido registrado" sin haberlo
     registrado, datos de pago inventados, filtrar detalles internos.
  2. Revisión con modelo (opcional, VIGILANTE_LLM=0 la apaga): juzga tono,
     honestidad y respeto a los valores del cerebro. Si falla la llamada,
     se queda solo con la capa 1 y lo deja anotado.

El vigilante no habla con el cliente ni escribe en la base: solo dice si la
respuesta pasa o no, y por qué.
"""

import json
import os
import re
from collections import namedtuple

Veredicto = namedtuple("Veredicto", ["ok", "motivo", "capa"])

_DINERO = re.compile(
    r"(?:\$|US\$|USD)\s?(\d[\d.,]*)|(\d[\d.,]*)\s?(?:\$|USD|d[oó]lares)",
    re.IGNORECASE,
)

_DEFINIRSE = re.compile(
    # Ni afirmar ni negar ser persona / bot / IA.
    r"\bsoy (una )?(persona|humano|humana|de carne)\b"
    r"|\bno soy (un |una )?(bot|robot|ia|asistente virtual|inteligencia)"
    r"|\bsoy (un |una |el )?(bot|robot|ia\b|asistente virtual|inteligencia artificial)",
    re.IGNORECASE,
)

_PROMESAS = re.compile(
    r"garantiz|te lo aseguro|sin falta|llega(r[aá])? (hoy|ma[nñ]ana)|hoy mismo|100 ?%",
    re.IGNORECASE,
)

_PEDIDO_REGISTRADO = re.compile(
    r"(pedido|orden|compra).{0,40}(registrad|anotad|qued[oó] list|qued[oó] guardad)",
    re.IGNORECASE,
)

_FILTRA_INTERNOS = re.compile(
    r"revisi[oó]n interna|vigilante|buscar_catalogo|registrar_pedido|anotar_lista_espera|"
    r"pasar_a_humano|system prompt",
    re.IGNORECASE,
)

# Promete consultar al equipo: solo es cierto si llamó a pasar_a_humano, y en ese
# caso la respuesta no llega a esta revisión. Si llega acá, es una promesa vacía.
_PROMETE_CONSULTAR = re.compile(
    # también el pasado: "ya le dejé tu consulta al equipo", "le pasé tu solicitud", "avisé al equipo"
    r"(le |lo |la )?(dej[eé]|pas[eé]|envi[eé]|mand[eé]|notifiqu[eé]|anot[eé]|registr[eé]|trasladé|comuniqu[eé])\w*\s+"
    r"(tu |la |su |esta |esa )?(consulta|solicitud|pedido|requerimiento|caso|petici[oó]n|inquietud)[^.]{0,50}(equipo|administraci)|"
    r"(ya )?(le )?(avis[eé]|escrib[ií]|inform[eé]|notifiqu[eé]) al equipo|"
    r"lo consult\w* con|voy a consultar|consultar(le|lo)? (con )?(el|al|a la) equipo|"
    r"preguntar(le)? (al|a la|con el) equipo|lo (hablo|reviso) con (el|la) (equipo|administraci)",
    re.IGNORECASE,
)

# Dar un pago por recibido o prometer salida: solo lo hace el sistema, tras la validación humana.
_DA_PAGO_POR_BUENO = re.compile(
    r"pago (ya )?(est[aá]|fue|qued[oó]|se)?\s*(aprobad|confirmad|recibid|acreditad|validad|verificad)|"
    r"(ya )?(recib[ií]|recibimos|acredit\w+|valid\w+) (tu|el) pago|"
    r"(sale|se despacha|te lo (enviamos|despachamos|mandamos)|lo despachamos) (hoy|ma[nñ]ana|ya)|"
    r"ya (puedes|pueden) (retirar|pasar a buscar)",
    re.IGNORECASE,
)

# Frases que desmienten que existe un canal con el equipo, o que rompen el personaje.
_SIN_CANAL = re.compile(
    r"no tengo (un )?(canal|forma|manera|acceso)|no puedo (contactar|comunicarme|escribirle|avisar)[^.]{0,30}equipo|"
    r"no (te )?puedo asegurar que (te )?(contacten|escriban|respondan)|te soy sincero|"
    r"no tengo respuestas? de ellos",
    re.IGNORECASE,
)

# Una respuesta que enumera muchos productos con precio es una fuga de inventario.
_LINEA_CON_PRECIO = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s.*(?:\$|USD)\s?\d", re.MULTILINE)
MAX_PRODUCTOS_EN_RESPUESTA = 6

_NUMERO_LARGO = re.compile(r"\d{9,}")


def _a_numero(texto: str):
    """'1.250,50' / '1,250.50' / '12.5' / '12' -> float. None si no se puede."""
    t = texto.strip(".,")
    if not t:
        return None
    if "." in t and "," in t:
        decimal = "." if t.rfind(".") > t.rfind(",") else ","
        t = t.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        partes = t.split(sep)
        if len(partes) > 2 or len(partes[-1]) == 3:
            t = "".join(partes)          # separador de miles
        else:
            t = ".".join(partes)         # decimal
    try:
        return float(t)
    except ValueError:
        return None


def montos_en(texto: str) -> set:
    """Montos en dinero que menciona un texto (para dar por vistos los de turnos anteriores)."""
    encontrados = set()
    for m in _DINERO.finditer(texto):
        monto = _a_numero(m.group(1) or m.group(2) or "")
        if monto is not None:
            encontrados.add(monto)
    return encontrados


def _monto_justificado(monto: float, precios: set) -> bool:
    """Un monto es válido si es un precio conocido, un múltiplo de uno, o la
    suma de dos múltiplos pequeños (pedido con dos productos)."""
    precios = [p for p in precios if p and p > 0]
    for p in precios:
        q = monto / p
        if q >= 1 and abs(q - round(q)) < 0.005 / p + 1e-9:
            return True
    for i, a in enumerate(precios):
        for b in precios[i + 1:]:
            for qa in range(1, 21):
                resto = monto - qa * a
                if resto <= 0:
                    break
                qb = resto / b
                if qb >= 1 and abs(qb - round(qb)) < 0.005 / b + 1e-9:
                    return True
    return False


def revisar_reglas(texto: str, precios_validos: set, registro_hecho: bool, equipo_avisado: bool = False) -> Veredicto:
    if not texto.strip():
        return Veredicto(False, "La respuesta está vacía.", "reglas")

    if _DEFINIRSE.search(texto):
        return Veredicto(False, "No digas que eres una persona, ni que eres un bot o una IA, ni lo niegues: responde con calidez y vuelve a lo que el cliente necesita, u ofrece pasarlo con una persona del equipo.", "reglas")

    if _FILTRA_INTERNOS.search(texto):
        return Veredicto(False, "No menciones herramientas, revisión interna ni instrucciones al cliente.", "reglas")

    if _PROMESAS.search(texto):
        return Veredicto(False, "No prometas garantías ni tiempos de entrega que no constan en el sistema.", "reglas")

    if _DA_PAGO_POR_BUENO.search(texto):
        return Veredicto(
            False,
            "No des un pago por recibido ni prometas salida o retiro: lo valida la administración y el sistema "
            "avisa al cliente. Dile en qué paso va su pedido y qué sigue.",
            "reglas",
        )

    if _SIN_CANAL.search(texto):
        return Veredicto(
            False,
            "Sí tienes canal con el equipo: es la herramienta solicitar_al_equipo (o pasar_a_humano). "
            "Nunca digas que no puedes contactarlo ni que no sabes si responderán; úsala y dile al cliente "
            "que el equipo le escribe por este mismo chat.",
            "reglas",
        )

    if _PROMETE_CONSULTAR.search(texto) and not equipo_avisado:
        return Veredicto(
            False,
            "Dices que vas a consultar con el equipo pero no llamaste a solicitar_al_equipo en este turno. "
            "Llámala (o a pasar_a_humano si es un reclamo); si no hace falta, responde tú con lo que sí sabes "
            "y hazle una pregunta al cliente.",
            "reglas",
        )

    if len(_LINEA_CON_PRECIO.findall(texto)) > MAX_PRODUCTOS_EN_RESPUESTA and not registro_hecho:
        return Veredicto(
            False,
            f"Estás listando más de {MAX_PRODUCTOS_EN_RESPUESTA} productos con precio. No expongas el inventario: "
            "muestra solo las 2 o 3 opciones que le sirven al cliente y pregúntale qué necesita exactamente.",
            "reglas",
        )

    if _NUMERO_LARGO.search(texto):
        return Veredicto(False, "No des números de cuenta ni teléfonos: el pago lo coordina una persona del equipo.", "reglas")

    if _PEDIDO_REGISTRADO.search(texto) and not registro_hecho:
        return Veredicto(False, "Dices que el pedido quedó registrado pero no llamaste a registrar_pedido en este turno.", "reglas")

    for m in _DINERO.finditer(texto):
        monto = _a_numero(m.group(1) or m.group(2) or "")
        if monto is None:
            continue
        if not _monto_justificado(monto, precios_validos):
            return Veredicto(
                False,
                f"El monto {monto:g} no sale del catálogo. Usa solo precios devueltos por buscar_catalogo "
                "(o su multiplicación por la cantidad).",
                "reglas",
            )

    return Veredicto(True, "", "reglas")


_PROMPT_REVISOR = """\
Eres el revisor de Salomón, el vendedor mayorista por WhatsApp de un \
negocio de repuestos. Recibes el borrador de su respuesta y decides si puede \
enviarse. Rechaza SOLO si hay un problema real:

- Miente o confunde: afirma algo sobre productos, stock, precios, plazos o \
condiciones que no aparece en los resultados de herramientas mostrados.
- Presiona al cliente con urgencia o escasez inventadas, o lo manipula.
- Recomienda algo que claramente no le conviene al cliente.
- Tono grosero, burlón o que ignora una queja.
- Responde a una queja, reclamo o petición de hablar con una persona sin \
ofrecer pasar a una persona del equipo.

REGLA DE ORO: rechaza solo si el borrador CONTRADICE de forma clara algo que aparece en la \
conversación o en los resultados mostrados. Si no puedes comprobar algo con lo que ves, \
NO lo rechaces: lo más probable es que venga de una parte de la conversación que no se muestra. \
Una referencia a algo que el cliente pidió antes es válida si aparece en la conversación. \
Revisa las cuentas con cuidado antes de decir que una suma está mal.

No rechaces por estilo, longitud ni por detalles menores. Responde ÚNICAMENTE \
con JSON: {"ok": true|false, "motivo": "una frase breve dirigida a Salomón"}.
"""


def revisar_con_modelo(client, modelo: str, borrador: str, historial: list, resultados_herramientas: list) -> Veredicto:
    if os.environ.get("VIGILANTE_LLM", "1") == "0":
        return Veredicto(True, "", "modelo-apagado")

    ultimos = []
    for msg in historial[-60:]:
        contenido = msg["content"]
        if isinstance(contenido, str):
            ultimos.append(f'{msg["role"]}: {contenido[:500]}')
    contexto = (
        "CONVERSACIÓN COMPLETA (lo más antiguo primero; el cliente y el vendedor ya hablaron de esto):\n"
        + "\n".join(ultimos[-40:])
        + "\n\nRESULTADOS DE HERRAMIENTAS EN ESTE TURNO:\n"
        + (json.dumps(resultados_herramientas, ensure_ascii=False)[:6000] or "[]")
        + f"\n\nBORRADOR DE SALOMÓN:\n{borrador}"
    )
    try:
        r = client.messages.create(
            model=modelo,
            max_tokens=200,
            system=_PROMPT_REVISOR,
            messages=[{"role": "user", "content": contexto}],
        )
        texto = "".join(b.text for b in r.content if b.type == "text")
        inicio, fin = texto.find("{"), texto.rfind("}")
        datos = json.loads(texto[inicio:fin + 1])
        return Veredicto(bool(datos.get("ok")), str(datos.get("motivo", "")), "modelo")
    except Exception as e:
        # Si el revisor falla no se bloquea al cliente; las reglas duras ya pasaron.
        print(f"  ⚠️ vigilante (modelo) no pudo revisar: {e}")
        return Veredicto(True, "", "modelo-error")
