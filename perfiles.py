"""
Registro de vendedores de ventas24 y enrutador.

Cada vendedor es un Perfil: quién es, de qué nivel, a qué tipos de contacto
atiende, qué cerebro (valores y reglas) usa, con qué modelo piensa y con qué
modelo lo vigila el revisor. Para sumar un vendedor de menor rango, se agrega
un Perfil nuevo a PERFILES (y su archivo de cerebro). El enrutador escoge el
primero de mayor nivel que atienda el tipo del contacto.

Sin perfil que lo atienda, el contacto va al vendedor de siempre (agente.py).

Variable de entorno VENDEDOR:
    (vacía)    todos con el vendedor de siempre
    auto       cada contacto con el vendedor de su tipo (Salomón → mayoristas)
    salomon    todos con Salomón, sea cual sea su tipo (útil para pruebas)
"""

import os
from dataclasses import dataclass

CARPETA = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Perfil:
    clave: str                       # id corto, el que se escribe en VENDEDOR
    nombre: str                      # cómo se llama el vendedor
    nivel: int                       # 1 = básico ... mayor número = más rango
    tipos: tuple                     # tipos de contacto que atiende ('mayorista', 'cliente')
    cerebro: str                     # archivo .md con valores, personalidad y reglas
    modelo: str = None               # None = el modelo base de agente.py
    modelo_vigilante: str = None     # None = VIGILANTE_MODELO o el modelo base
    max_reintentos: int = 2          # veces que el vigilante puede devolver una respuesta antes de pasar a humano


SALOMON = Perfil(
    clave="salomon",
    nombre="Salomón",
    nivel=3,                         # Premium
    tipos=("mayorista",),
    cerebro=os.path.join(CARPETA, "salomon_cerebro.md"),
    modelo="claude-sonnet-5-5",
    modelo_vigilante="claude-haiku-4-5-20251001",
)

# Para agregar uno de menor rango más adelante, por ejemplo:
#   BASICO = Perfil(clave="basico", nombre="...", nivel=1, tipos=("cliente",),
#                   cerebro=os.path.join(CARPETA, "basico_cerebro.md"))
PERFILES = (SALOMON,)


def perfil_por_clave(clave: str):
    return next((p for p in PERFILES if p.clave == clave), None)


def perfil_para(tipo: str):
    """El perfil de mayor nivel que atiende ese tipo de contacto, o None."""
    candidatos = [p for p in PERFILES if tipo in p.tipos]
    return max(candidatos, key=lambda p: p.nivel) if candidatos else None


def Agente(api_key=None, telefono=None, tipo="cliente"):
    """Fábrica con la misma firma que agente.Agente: devuelve el vendedor que corresponde."""
    import agente as legacy
    import db

    modo = os.environ.get("VENDEDOR", "").strip().lower()
    if not modo:
        return legacy.Agente(api_key=api_key, telefono=telefono, tipo=tipo)

    perfil = None
    if modo == "auto":
        id_contacto = db.obtener_o_crear_contacto(telefono or "terminal-sin-numero", tipo=tipo)
        with db.conectar() as con:
            fila = con.execute("SELECT tipo FROM contactos WHERE id = ?", (id_contacto,)).fetchone()
        perfil = perfil_para(fila["tipo"] if fila else tipo)
    else:
        perfil = perfil_por_clave(modo)

    if perfil is None:
        return legacy.Agente(api_key=api_key, telefono=telefono, tipo=tipo)

    from salomon import Salomon
    return Salomon(api_key=api_key, telefono=telefono, tipo=tipo, perfil=perfil)
