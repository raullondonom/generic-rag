# Respaldos de las bases de datos PostgreSQL

Las bases de datos de producción (pedidos_db, pagos_db e inventario_db) se respaldan con **pgBackRest**.

## Calendario
- **Respaldo completo: todos los domingos a las 02:00.**
- Respaldo incremental: de lunes a sábado a las 02:00.
- Además se archiva el WAL de forma continua, lo que permite restaurar a un punto en el tiempo.

## Retención y almacenamiento
Los respaldos se guardan en el bucket `andina-backups-prod` con **retención de 35 días**. Una copia
semanal se replica a una segunda región.

## Objetivos
- RPO (pérdida máxima de datos): 15 minutos gracias al archivado del WAL.
- RTO (tiempo máximo de recuperación): 4 horas.

## Pruebas de restauración
El primer lunes de cada mes el equipo de Plataforma restaura el último respaldo completo en el ambiente
de pruebas y verifica que la aplicación arranque. El resultado se registra en la bitácora de DR.

## Responsables
El dueño de este procedimiento es el equipo de Plataforma. Cualquier cambio en el calendario debe
pasar por el comité de cambios.
