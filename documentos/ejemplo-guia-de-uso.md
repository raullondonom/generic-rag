# Guía de uso del asistente RAG

> Documento de ejemplo incluido en el repositorio para probar el asistente sin configurar Confluence.
> Pregunta, por ejemplo: "¿Cómo agrego documentos nuevos?" o "¿Quién es el responsable del asistente?".

## ¿Qué es?
El asistente RAG responde preguntas usando únicamente los documentos de la carpeta `documentos/`
y las páginas de los espacios de Confluence configurados en `CONFLUENCE_SPACES`.

## ¿Cómo agrego documentos?
1. Copia los archivos a la carpeta `documentos/` (se admiten subcarpetas).
2. Espera hasta 60 segundos: el sistema detecta los archivos nuevos o modificados y los indexa.
3. También puedes pulsar **Reindexar carpeta** en el panel lateral.

## ¿Cómo cambio de modelo de IA?
En el selector "Proveedor de IA" del panel lateral se puede elegir entre:
- **Ollama (local)**: modelo qwen3:8b ejecutándose en la propia máquina (GPU o CPU).
- **Claude (Anthropic)**: modelo remoto; requiere una API key de Anthropic.

## Soporte
Responsable del asistente: equipo de Plataforma. Horario de soporte: lunes a viernes de 8:00 a 17:00.
Las incidencias se registran enviando un correo a la mesa de ayuda con el asunto "RAG".
