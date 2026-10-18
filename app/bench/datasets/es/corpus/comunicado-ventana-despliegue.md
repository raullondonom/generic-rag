# Comunicado de Operaciones: nueva ventana de despliegue (septiembre de 2026)

Fecha de publicación: 15 de septiembre de 2026.

A partir del **1 de octubre de 2026** cambia la ventana de despliegue a producción de **pedidos-api**:
los despliegues pasan a hacerse **solo los miércoles, entre las 21:00 y las 22:30** (hora de Bogotá).

Esta ventana **reemplaza** la de martes y jueves que aparece en el runbook de despliegue, que se
actualizará en las próximas semanas. Las demás reglas del runbook (dos aprobaciones, canary del 10 % y
procedimiento de rollback) no cambian.

El motivo del cambio es que los martes y jueves por la noche coinciden con el cierre contable de
facturación, que consume la mayor parte de la capacidad de la base de datos.

Dudas: canal #operaciones.
