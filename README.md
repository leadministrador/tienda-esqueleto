# Tienda esqueleto

Base para crear tiendas online con panel de administración. Cada tienda usa este mismo código; solo cambian sus datos.

## Qué incluye (versión 1)
- Tienda pública: catálogo con filtros, ficha de producto con fotos y video de YouTube, carrito y pedido por WhatsApp.
- Panel en `/admin`:
  - **Productos**: nombre, descripción, precio, precio anterior (oferta), categoría, varias fotos, video de YouTube, visible u oculto.
  - **Mi tienda** (solo dueño): nombre, rubro, logo, colores, portada, WhatsApp, Instagram, horario, dirección.
  - **Cuentas** (solo dueño): dar o quitar permiso de ayudante.
- Cuentas con celular + contraseña. La primera cuenta que se crea es la del dueño.
- Fotos guardadas en alta calidad (2000 px) más una copia chica (600 px) para el catálogo.

## Variables de entorno (Render > Environment)
| Nombre | Qué es |
|---|---|
| `DATABASE_URL` | Link de conexión de Neon. Sin esto los datos se borran. |
| `SECRET_KEY` | Texto largo al azar. Protege las sesiones. |

Opcional (fotos fuera de la base de datos, sin probar todavía): `S3_BUCKET`, `S3_ENDPOINT`, `S3_KEY`, `S3_SECRET`, `S3_PUBLIC_URL`, `S3_REGION`.

## Render (Web Service)
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app -w 1 --threads 4 -t 60`

## Pendiente (próximas versiones)
Ofertas y cupones, concursos, reseñas, botón de Google, notificaciones push, copia diaria en GitHub, Mercado Pago.
