"""
Prueba el vendedor de voz por terminal, sin Twilio: escribes lo que el cliente diría y ves lo que el
sintetizador diría. No envía nada por WhatsApp (modo simulación).

Uso:
    CRM_DB_PATH=./crm_voz_prueba.db python probar_voz.py [telefono]

Requiere ANTHROPIC_API_KEY. Usa un teléfono de prueba, nunca el de un cliente real.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

import voz


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Falta ANTHROPIC_API_KEY (variable de entorno o archivo .env).")
        return
    telefono = sys.argv[1] if len(sys.argv) > 1 else "+580000000000"
    callsid = "SIM" + uuid.uuid4().hex[:12]
    print("Llamada de prueba (simulación, no se envía nada por WhatsApp). Escribe 'salir' para colgar.\n")
    print("🔊", voz.voz_vigilante.para_hablar(voz.iniciar(telefono, callsid)))
    while True:
        try:
            texto = input("\n🎤 Cliente: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto.lower() in ("salir", "chao", "exit"):
            break
        if not texto:
            continue
        r = voz.responder(telefono, callsid, texto, simulacion=True)
        print("🔊", r["texto"])
        for w in r["whatsapp"]:
            print("   📱 (saldría por WhatsApp):", w.replace("\n", " ")[:200])
        if r["colgar"]:
            print("   (el vendedor cuelga)")
            break
    voz.terminar(callsid)


if __name__ == "__main__":
    main()
