# Arquitectura del servicio pagos-core

**pagos-core** es el servicio que cobra los envíos. Está escrito en Go y se comunica con la pasarela de
pagos externa **PayNova**.

## Flujo
1. pedidos-api publica un mensaje en la cola RabbitMQ `pagos.pendientes`.
2. pagos-core consume el mensaje y llama a la API de PayNova.
3. Al confirmar el cobro, pagos-core publica en la cola `pagos.confirmados`.
4. Facturación consume `pagos.confirmados` y emite la factura electrónica.

## Llamadas a la pasarela
- Cada llamada a PayNova tiene un **timeout de 8 segundos**.
- Si falla, se hacen **3 reintentos con backoff exponencial** (1 s, 2 s y 4 s).
- Todas las llamadas llevan el encabezado `X-Idempotency-Key` con el id del pedido, para que un
  reintento nunca cobre dos veces.

## Fallos persistentes
Si después de los reintentos el cobro sigue fallando, el mensaje va a la cola de errores
`pagos.dlq` y se genera una alerta para el equipo de Pagos. Esos mensajes se reprocesan a mano.

## Base de datos
pagos-core guarda el estado de cada cobro en `pagos_db`. Los datos de tarjetas nunca se guardan: los
tokeniza PayNova.
