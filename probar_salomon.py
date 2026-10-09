"""
Prueba a Salomón por terminal, simulando una conversación de WhatsApp.

Uso:
    python probar_salomon.py [telefono]

Para no tocar los datos reales, usa una base de prueba aparte:
    CRM_DB_PATH=./crm_salomon_prueba.db python probar_salomon.py

Requiere ANTHROPIC_API_KEY (variable de entorno o archivo .env).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

from salomon import Salomon


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  No encontré ANTHROPIC_API_KEY (variable de entorno o archivo .env).")
        return

    telefono = sys.argv[1] if len(sys.argv) > 1 else "terminal-salomon"
    salomon = Salomon(telefono=telefono)
    print(f"(hablando con Salomón como contacto: {telefono}. Escribe 'salir' para terminar)\n")

    while True:
        try:
            mensaje = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n¡Hasta luego!")
            break
        if not mensaje:
            continue
        if mensaje.lower() in ("salir", "exit", "quit"):
            print("¡Hasta luego!")
            break
        print(f"Salomón: {salomon.procesar_mensaje(mensaje)}\n")


if __name__ == "__main__":
    main()
