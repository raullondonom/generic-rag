# Runbook: despliegue de pedidos-api

Este documento describe cómo se publica una nueva versión del servicio **pedidos-api**, que recibe y
valida los pedidos de los clientes de Andina Logística.

## Ventana de despliegue
Los despliegues a producción solo se hacen los **martes y jueves entre las 22:00 y las 23:30** (hora de
Bogotá). Fuera de esa ventana se necesita una excepción aprobada por el gerente de operaciones.

## Aprobaciones
Cada release necesita dos aprobaciones en el pipeline `release-pedidos` de Jenkins: la del líder técnico
del equipo y la de QA. Sin las dos aprobaciones el pipeline no avanza a la etapa de producción.

## Pasos
1. Crear el tag de la versión en el repositorio (`vX.Y.Z`).
2. Ejecutar el pipeline `release-pedidos` con el parámetro `AMBIENTE=prod`.
3. El pipeline publica primero un **canary con el 10 % del tráfico durante 15 minutos**.
4. Si el endpoint de salud `/salud` responde bien y la tasa de errores se mantiene bajo el 1 %, el
   pipeline completa el despliegue al 100 %.

## Rollback
Si algo falla después del despliegue, el rollback se hace con:

```
kubectl rollout undo deployment/pedidos-api -n prod
```

Después del rollback hay que avisar en el canal #incidentes y abrir un ticket en la mesa de cambios.
El servicio guarda los pedidos en la base de datos `pedidos_db`, por lo que un rollback de código no
revierte cambios de esquema: las migraciones deben ser compatibles hacia atrás durante dos versiones.
