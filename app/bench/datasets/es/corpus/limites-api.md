# Límites de uso de la API pública

La API pública de Andina Logística permite a los clientes consultar el estado de sus envíos.

## Límites por plan
| Plan        | Peticiones por minuto | Ráfaga máxima |
|-------------|-----------------------|---------------|
| Estándar    | 100 por token         | 20            |
| Empresarial | 1000 por token        | 200           |

Los límites se calculan con una **ventana deslizante de 60 segundos**.

## Cuando se supera el límite
La API responde con el código **HTTP 429 (Too Many Requests)** e incluye el encabezado **Retry-After**
con los segundos que hay que esperar antes de volver a intentar. Las peticiones rechazadas no cuentan
para el límite.

## Buenas prácticas
- Respeta el valor de Retry-After en lugar de reintentar de inmediato.
- Usa los webhooks de cambio de estado en lugar de consultar cada envío en un ciclo.
- Para subir de plan escribe a comercial@andina.example.

## Autenticación
Cada petición debe llevar el token en el encabezado `Authorization: Bearer <token>`. Los tokens se
generan en el portal de clientes y se pueden revocar en cualquier momento.
