# Postmortem: caída de pedidos-api del 14 de marzo de 2026

## Resumen
El 14 de marzo de 2026 pedidos-api estuvo fuera de servicio durante **47 minutos** (de 10:12 a 10:59).
Los clientes no pudieron registrar pedidos y unos 1.800 pedidos quedaron en espera.

## Causa raíz
El **certificado TLS del balanceador de carga expiró**. Nadie recibió aviso porque el monitoreo solo
revisaba la disponibilidad del servicio por dentro del clúster, donde no se usa ese certificado.

## Detección y resolución
La caída se detectó por las quejas de los clientes en el canal de soporte, 9 minutos después de
empezar. El equipo de Plataforma renovó el certificado y reinició el balanceador.

## Acciones correctivas
1. Crear **alertas que avisen 30 días antes de que expire cualquier certificado**. Responsable: equipo
   de Plataforma. Estado: completada.
2. Agregar un chequeo de disponibilidad externo, desde fuera de la red. Estado: en curso.
3. Documentar la renovación de certificados en el runbook del balanceador. Estado: completada.

## Lecciones aprendidas
Los chequeos de salud internos no detectan fallos de la capa de entrada. Hay que monitorear también lo
que ve el cliente.
