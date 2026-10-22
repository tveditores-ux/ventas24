"""
Vigilante del canal de voz. Se suma a las reglas de salomon_vigilante (precios inventados, promesas,
pago dado por recibido, etc.) con lo que solo importa cuando la respuesta se LEE EN VOZ ALTA:

  - corta (se oye, no se lee),
  - sin formato (listas, viñetas, asteriscos, emojis, enlaces),
  - sin datos de pago ni números largos dictados por teléfono,
  - una sola pregunta por turno.

También prepara el texto para el sintetizador (`para_hablar`): montos en palabras, porcentajes,
códigos de producto deletreados con pausas. No revisa con modelo salvo VOZ_VIGILANTE_LLM=1, porque
en una llamada cada segundo de espera se nota.
"""

import re

from salomon_vigilante import Veredicto

MAX_PALABRAS = 75

_FORMATO = re.compile(r"(^|\n)\s*(?:[-•*#]|\d+[.)])\s|[*_`#>]{1,}|https?://|www\.|\S+@\S+\.\S+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")
_PAGO = re.compile(
    r"pago m[oó]vil|zelle|binance|transferencia|n[uú]mero de cuenta|cuenta (corriente|de ahorro|n[uú]mero)|"
    r"titular|c[eé]dula|\bRIF\b|\b0?4(12|14|16|24|26)[\s-]?\d{3}",
    re.IGNORECASE,
)
_DIGITOS_LARGOS = re.compile(r"\d[\d\s.-]{8,}\d")


def revisar(texto: str) -> Veredicto:
    """Reglas propias de la voz. Devuelve ok=True si la respuesta se puede decir tal cual."""
    t = (texto or "").strip()
    if not t:
        return Veredicto(False, "La respuesta está vacía.", "voz")
    if _FORMATO.search(t) or _EMOJI.search(t):
        return Veredicto(False, "Se va a leer en voz alta: quita listas, viñetas, asteriscos, emojis y enlaces. Habla en frases corridas.", "voz")
    if _PAGO.search(t):
        return Veredicto(False, "No dictes datos de pago, cuentas, cédulas ni teléfonos por voz: di que se los mandas por WhatsApp.", "voz")
    if _DIGITOS_LARGOS.search(t):
        return Veredicto(False, "No dictes números largos por teléfono: ofrece mandarlos por WhatsApp.", "voz")
    if len(t.split()) > MAX_PALABRAS:
        return Veredicto(False, f"Es muy largo para decirlo por teléfono ({len(t.split())} palabras). Máximo dos o tres frases cortas; el resto lo dices en el siguiente turno.", "voz")
    if t.count("?") + t.count("¿") > 4:   # cada pregunta lleva ¿ y ?
        return Veredicto(False, "Haz una sola pregunta por turno.", "voz")
    return Veredicto(True, "", "voz")


_MONTO = re.compile(r"(?:US\$|\$)\s?(\d+)(?:[.,](\d{1,2}))?(?!\d)")
_CODIGO = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9-]{5,}\b")


def _monto(m):
    enteros, cent = m.group(1), m.group(2)
    texto = f"{enteros} dólares"
    if cent and int(cent) > 0:
        texto += f" con {int(cent.ljust(2, chr(48)))} centavos"
    return texto


def _deletrear(m):
    return ", ".join(" ".join(p) if p.isalnum() else "guion" for p in re.split(r"(-)", m.group(0)) if p)


def para_hablar(texto: str) -> str:
    """Prepara el texto para el sintetizador de voz."""
    t = _MONTO.sub(_monto, texto)
    t = t.replace("%", " por ciento").replace("&", " y ")
    t = _CODIGO.sub(_deletrear, t)
    t = re.sub(r"\s+", " ", t).strip()
    return t
