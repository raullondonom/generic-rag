# Guía de acceso remoto (VPN)

Para trabajar fuera de la oficina se usa la VPN corporativa basada en **WireGuard**.

## Configuración
- Servidor: `vpn.andina.local`
- Puerto: **51820/UDP**
- Cada área tiene un perfil distinto (operaciones, tecnología, administrativo). El perfil define a qué
  redes internas se puede llegar.

## Autenticación
Además del archivo de configuración, la conexión pide un segundo factor desde la aplicación
**Andina Auth** del celular. Los certificados de la VPN **caducan cada 90 días** y se renuevan desde el
portal de autoservicio de tecnología.

## Problemas frecuentes
- Si la conexión se queda en "handshake", revisa que la red de tu casa no bloquee el puerto UDP.
- Si el certificado caducó, la VPN conecta pero no llega a ningún servicio interno.

## Soporte
Si no logras conectarte, llama a la Mesa de Ayuda a la **extensión 4410** o escribe en el canal
#soporte-ti. El horario de atención es de lunes a viernes de 7:00 a 19:00.
