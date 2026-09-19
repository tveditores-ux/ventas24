# Ventas 24 — agente de ventas por WhatsApp con CRM

Sistema de ventas automatizado por WhatsApp para filtros y repuestos
vehiculares. Usa la API de Anthropic (Claude) con function calling real
(el modelo nunca inventa precio ni stock: siempre pasa por el catálogo
real) y guarda todo — catálogo, conversaciones, lista de espera y
pedidos — en una base de datos SQLite (`crm.db`) conectada por contacto.

## Componentes

| Archivo | Qué hace |
|---|---|
| `db.py` | Base de datos (SQLite). Tablas: `contactos`, `interacciones`, `solicitudes_espera`, `pedidos`, `catalogo`. |
| `agente.py` | El agente: llama a Claude, ejecuta las herramientas (`buscar_catalogo`, `anotar_lista_espera`, `registrar_pedido`) y guarda el historial por contacto en `crm.db`. |
| `probar_agente.py` | Chat de prueba por terminal. `python probar_agente.py [telefono]` — sin argumento simula un cliente fijo; con un número simulás un contacto distinto. |
| `app_whatsapp.py` | Servidor Flask que conecta el agente a WhatsApp real vía el webhook de Twilio. |
| `migrar_datos.py` | Importa datos viejos de CSV (si existen) a `crm.db`. Se puede correr de nuevo sin duplicar. |
| `campana_whatsapp.py` | Manda una plantilla de WhatsApp aprobada a los clientes al mayor nuevos. |
| `llamar_mayoristas.py` | Llama por teléfono a los clientes al mayor nuevos (requiere número de Twilio con voz). |

## 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

## 2. Configurar `.env`

Copiá `.env.example` como `.env` y completá al menos:

```
ANTHROPIC_API_KEY=sk-ant-tu-clave-real
```

Para WhatsApp real y la campaña al mayor, además:

```
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_VOICE_FROM=...        # número de Twilio con voz, para llamar_mayoristas.py
TWILIO_TEMPLATE_SID=...      # plantilla de WhatsApp aprobada por Meta, para campana_whatsapp.py
```

## 3. Probar el agente en tu terminal

```bash
python probar_agente.py
```

El historial de esta conversación queda guardado en `crm.db` — si volvés
a correr el script (o reiniciás la compu), el agente se acuerda de todo
lo hablado con ese contacto.

## 4. Base de datos (`crm.db`)

Se crea sola la primera vez que se importa `db.py`. Si venís de una
versión anterior del proyecto (con `catalogo_50_modelos_venezuela.csv`,
`lista_espera.csv` o `clientes_mayoristas.csv`), corré una vez:

```bash
python migrar_datos.py
```

Para cargar o actualizar el catálogo directamente en la base (por
ejemplo, para poner precios y stock reales en vez de 0), usá
`db.insertar_producto(...)` desde un script chico, o reemplazá la tabla
`catalogo` por tu fuente real (Google Sheet, etc.) editando la función
`buscar_catalogo()` en `db.py`.

## 5. Correr el servidor de WhatsApp

```bash
python app_whatsapp.py
```

Necesita el puerto 5000 expuesto públicamente (por ejemplo con `ngrok
http 5000`) para que Twilio pueda llamarlo. Instrucciones completas de
cómo conectar el sandbox de WhatsApp de Twilio: ver la conversación del
proyecto o pedirle a Claude que te guíe de nuevo.

## 6. Venta al mayor

`clientes_mayoristas.csv` ya no se usa — los contactos al mayor viven en
`crm.db` (tabla `contactos`, `tipo = 'mayorista'`). Para cargar uno
nuevo desde Python:

```python
import db
id_contacto = db.obtener_o_crear_contacto("+58...", tipo="mayorista")
db.actualizar_contacto(id_contacto, nombre="...", negocio="...", tipo_negocio="...", ciudad="...")
```

Con clientes cargados y estado `nuevo`, `campana_whatsapp.py` y
`llamar_mayoristas.py` los toman automáticamente (ver los requisitos de
cada uno al inicio de su archivo).

## Estado actual / pendientes conocidos

- Precio y stock del catálogo siguen en 0 para la mayoría de los
  productos — hay que cargar los reales.
- El dashboard (Artifact publicado aparte) todavía lee de una foto fija
  de datos, no de `crm.db` en vivo — hay que decidir cómo conectarlo si
  se necesita.
- Las llamadas de voz salientes (`llamar_mayoristas.py`) funcionan pero
  pueden no sonar en el teléfono del cliente si el número de Twilio es
  nuevo (problema de reputación de número, no del código).
