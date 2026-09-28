# Ventas 24 — canal de Mercado Libre

Módulo **independiente** del agente de WhatsApp. No modifica ningún archivo
existente: tiene su propio servidor, su propia base de datos (`meli.db`) y
corre como un servicio aparte en Railway. Si algo falla acá, WhatsApp y el
CRM siguen funcionando igual.

## Qué hace

1. **Preguntas**: cuando alguien pregunta en una publicación, Claude
   redacta la respuesta con los datos reales de la publicación. Si todo
   está bien, la publica. Si no está seguro, o la respuesta trae algo
   prohibido por Mercado Libre (teléfono, WhatsApp, enlaces, correos), la
   deja pendiente en el panel y te avisa.
2. **Venta**: cuando alguien compra, te avisa por WhatsApp y le escribe al
   comprador por la mensajería de la orden: le agradece, le pasa tus
   datos de pago y le pide los datos de envío.
3. **Pago**: cuando el comprador dice que pagó (método, referencia,
   monto), el bot lo registra, le dice que estás verificando y **te avisa
   por WhatsApp**. El bot nunca confirma un pago por su cuenta.
4. **Tú verificas** en el panel (`/meli/panel`):
   - **Confirmar pago** → le avisa al comprador y, si faltan, le pide los
     datos de envío.
   - **No aparece** → le pide revisar la referencia o enviar la captura.
   - **Marcar como enviada** (empresa + guía) → le manda la guía.
5. Si el comprador reclama o pide algo raro, el bot se pausa en esa orden
   y te avisa. Puedes escribirle tú desde el panel.

## Archivos

| Archivo | Qué hace |
|---|---|
| `app_meli.py` | Servidor: webhook de Mercado Libre, autorización y API del panel. |
| `agente_meli.py` | El agente (preguntas + postventa) y el filtro de seguridad. |
| `meli_api.py` | Llamadas a Mercado Libre y renovación automática del token. |
| `meli_db.py` | Base de datos propia `meli.db`. |
| `avisos.py` | Te avisa por WhatsApp (WeCall → Twilio) y deja todo en el panel. |
| `panel.html` | Panel web para aprobar, verificar pagos y marcar envíos. |

## Puesta en marcha

### 1. Crear la app en Mercado Libre
En https://developers.mercadolibre.com.ve → *Mis aplicaciones* → *Crear aplicación*:
- **URI de redirect**: `https://TU-SERVICIO.up.railway.app/meli/callback`
- **Permisos (scopes)**: lectura, escritura y `offline_access`.
- **Tópicos de notificaciones**: `questions`, `orders_v2`, `messages`.
- **URL de notificaciones**: `https://TU-SERVICIO.up.railway.app/meli/notificaciones`

Anota el **App ID** y la **Secret Key**.

### 2. Crear el servicio en Railway (aparte del de WhatsApp)
En el mismo proyecto: *New → GitHub Repo →* el mismo repositorio.
- *Settings → Config-as-code*: `mercadolibre/railway.json`
- *Settings → Volumes*: montar un volumen en `/data`
- *Variables*: las de `.env.meli.example`, más `ANTHROPIC_API_KEY` (y
  `WECALL_API_KEY` / `TWILIO_*` si quieres los avisos por WhatsApp).
- Genera un dominio público y úsalo en el paso 1.

### 3. Conectar tu cuenta
Abre `https://TU-SERVICIO.up.railway.app/meli/conectar?token=TU_MELI_PANEL_TOKEN`,
autoriza con tu cuenta de vendedor y listo.

### 4. Probar antes de soltarlo
- En el panel, usa **Probar una respuesta** con el ID de una publicación
  tuya (no publica nada).
- Arranca con `MELI_MODO_PREGUNTAS=aprobar` y `MELI_MODO_POSTVENTA=aprobar`.
  Cuando veas que responde bien, cámbialos a `auto`.

### Local (opcional)
```bash
python -m mercadolibre.app_meli
```
Levanta en el puerto 5055. Para recibir notificaciones reales necesitas
exponerlo con ngrok y poner esa URL en la app de Mercado Libre.

## Cosas a tener en cuenta

- **Avisos por WhatsApp**: WhatsApp solo permite mensajes libres si le
  escribiste a ese número en las últimas 24 h. Si no, el aviso no sale por
  WhatsApp, pero **siempre** queda en el panel (sección *Avisos*).
- **Datos de pago con teléfono o correo**: Pago Móvil y Zelle llevan
  teléfono o correo. El bot no los escribe; los inserta el sistema desde
  `MELI_DATOS_PAGO`. Mercado Libre podría moderar esos mensajes; si pasa,
  aparecerá como error en el panel.
- Las órdenes anteriores a la instalación no se tocan.
- Si el servidor estuvo caído, el botón **Buscar preguntas sin responder**
  atiende las que quedaron pendientes.
