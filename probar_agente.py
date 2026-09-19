"""
Prueba el agente por terminal, simulando una conversación de WhatsApp.

Uso:
    python probar_agente.py

Requiere la variable de entorno ANTHROPIC_API_KEY configurada
(o un archivo .env con esa variable, si usas python-dotenv).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from agente import Agente

load_dotenv()

SALUDO = (
    "¡Hola! 👋 Gracias por escribir. Somos tu proveedor de filtros y "
    "repuestos. ¿Qué filtro o artículo estás buscando y para qué vehículo?"
)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  No encontré la variable ANTHROPIC_API_KEY.")
        print("    Crea un archivo .env con: ANTHROPIC_API_KEY=tu_clave_aqui")
        return

    telefono = sys.argv[1] if len(sys.argv) > 1 else "terminal-test"
    agente = Agente(telefono=telefono)
    print(f"(probando como contacto: {telefono} — pasá otro número como argumento para simular otro cliente)")
    print(f"Agente: {SALUDO}\n")

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

        respuesta = agente.procesar_mensaje(mensaje)
        print(f"Agente: {respuesta}\n")


if __name__ == "__main__":
    main()
