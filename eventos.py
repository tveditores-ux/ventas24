"""
Bitácora en memoria del flujo de un mensaje por el sistema, para poder
verlo en vivo en /monitor. No se guarda en crm.db a propósito —es
telemetría de "qué está pasando ahora", no historial de negocio— así
que se reinicia en cada deploy o reinicio del proceso, y eso está bien.
"""

import itertools
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_eventos = deque(maxlen=400)
_contador = itertools.count(1)

ETAPAS = (
    "webhook_recibido",
    "firma_invalida",
    "dedup_bloqueado",
    "modo_manual",
    "contexto_wecall",
    "agente_iniciado",
    "claude_llamado",
    "herramienta",
    "respuesta_generada",
    "envio_wecall",
    "error",
)


def registrar(etapa: str, telefono: str = None, detalle: str = "", ok: bool = True, agente_tipo: str = None):
    evento = {
        "id": next(_contador),
        "ts": datetime.now(timezone.utc).isoformat(),
        "etapa": etapa,
        "telefono": telefono,
        "detalle": detalle,
        "ok": ok,
        "agente_tipo": agente_tipo,
    }
    with _lock:
        _eventos.append(evento)
    return evento


def listar(desde_id: int = 0):
    with _lock:
        return [e for e in _eventos if e["id"] > desde_id]
