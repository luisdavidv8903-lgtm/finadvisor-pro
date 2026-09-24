# Chambeando — Public Activation Checklist

Este documento cubre el **único paso pendiente** para que
`chambeando-v4-webhook.link-credit.com` sirva tráfico público real desde el
VPS, en vez del túnel de Cloudflare antiguo que hoy devuelve HTTP 530.

**Nada de este documento se ha ejecutado.** El cambio de DNS descrito abajo
fue autorizado por el usuario en la conversación, pero explícitamente
**no debe ejecutarse todavía** — queda documentado, listo para cuando se dé
la orden de ejecutar.

Estado previo confirmado (ver `chambeando/SERVER_DEPLOYMENT_RUNBOOK.md`):
Chambeando corre internamente en `127.0.0.1:8100` (health PASS), el bloque
Caddy para este hostname ya está agregado y recargado, y Barberbot sigue
intacto en el puerto 3001. Lo único que falta es que el DNS del subdominio
deje de apuntar al túnel de Cloudflare y apunte al VPS.

---

## 1. Cambiar el registro DNS (único cambio externo necesario)

```
Hostname:  chambeando-v4-webhook.link-credit.com
De:        registro de Cloudflare Tunnel (actual, produce HTTP 530)
A:         registro tipo A
Valor:     159.89.231.213
Modo:      "Solo DNS" (DNS only -- proxy de Cloudflare DESACTIVADO para
           este hostname, naranja apagado) -- Caddy en el VPS ya maneja
           TLS/HTTPS directamente, no se quiere el proxy de Cloudflare
           interponiéndose ni cacheando nada aquí.
TTL:       Automático
```

**Ámbito del cambio**: exactamente este único hostname
(`chambeando-v4-webhook.link-credit.com`). Ningún otro registro de
`link-credit.com` se toca — en particular, `bot.link-credit.com`
(Barberbot) no se modifica de ninguna forma.

---

## 2. Esperar propagación

TTL automático de Cloudflare suele ser bajo (minutos), pero la propagación
de DNS a nivel global/resolvers de terceros puede tardar más. No hay una
acción a ejecutar aquí más que esperar y volver a verificar (paso 3).

---

## 3. Verificar tras la propagación

Todos los comandos siguientes son de solo lectura — verifican el estado
público, no cambian nada.

```bash
# 3a. DNS resuelve al VPS
dig +short chambeando-v4-webhook.link-credit.com
# Esperado: 159.89.231.213 (y nada más -- si aparece una IP de rango
# Cloudflare, el proxy sigue activo o el cambio no propagó todavía)

# 3b. HTTPS público responde /health -- Caddy debe emitir su propio
# certificado automáticamente para este hostname una vez el DNS resuelva
# aquí (Caddy usa HTTP-01/TLS-ALPN por defecto, no requiere ninguna acción
# manual de certificado si el puerto 443 es alcanzable, que ya lo es porque
# Barberbot/bot.link-credit.com ya sirve HTTPS desde el mismo Caddy).
curl -sS -w "\nHTTP:%{http_code}\n" https://chambeando-v4-webhook.link-credit.com/health
# Esperado: {"status":"ok","app":"chambeando-backend"}  HTTP:200

# 3c. Confirmar que Barberbot sigue funcionando exactamente igual
curl -sS -o /dev/null -w "bot.link-credit.com -> HTTP:%{http_code}\n" https://bot.link-credit.com/
# Esperado: el mismo código que respondía ANTES de este cambio -- cualquier
# diferencia es una señal de que algo en Caddy se vio afectado, y debe
# investigarse antes de seguir.

# 3d. (opcional, más detalle) TLS del nuevo hostname
echo | openssl s_client -connect chambeando-v4-webhook.link-credit.com:443 \
  -servername chambeando-v4-webhook.link-credit.com 2>/dev/null \
  | openssl x509 -noout -dates -subject
```

Checklist:
- [ ] `dig` resuelve a `159.89.231.213` (sin IP de Cloudflare).
- [ ] `GET https://chambeando-v4-webhook.link-credit.com/health` responde
      `200` con el JSON esperado.
- [ ] `bot.link-credit.com` (Barberbot) sigue respondiendo exactamente
      igual que antes del cambio de DNS.
- [ ] `chambeando/scripts/verify_vps_systemd.sh` sigue reportando
      `VERIFY_GATE=PASS` (confirma que nada del lado del servidor se
      degradó por el cambio de DNS en sí).

Si cualquiera de estos falla, ver la sección de rollback antes de continuar.

---

## 4. SOLO después de que 3 esté en verde: considerar Meta webhook

**No antes.** Configurar el webhook de Meta contra un endpoint que todavía
no resuelve públicamente, o que resuelve pero no ha sido verificado de
punta a punta, arriesga que Meta marque el webhook como fallido
repetidamente (con las implicaciones de salud de la app de Meta que eso
tiene) sin ganar nada.

Una vez el paso 3 esté completamente verde:
1. Confirmar que `WHATSAPP_APP_SECRET` y `WHATSAPP_VERIFY_TOKEN` en
   `/etc/chambeando/chambeando.env` son valores reales (no los placeholders
   públicos) — ver `DEPLOYMENT_RUNBOOK.md` sección D sobre por qué el
   webhook falla cerrado (503) mientras `WHATSAPP_APP_SECRET` siga siendo
   el default público, exactamente como salvaguarda para este momento.
2. Registrar `https://chambeando-v4-webhook.link-credit.com/whatsapp/webhook`
   en el dashboard de Meta, siguiendo `DEPLOYMENT_RUNBOOK.md` sección G.
3. Esto es una acción sobre Meta -- fuera del alcance de esta sesión y de
   este documento (esta sesión tiene instrucción explícita de no tocar
   Meta). Ejecutarla es responsabilidad del operador, en su propia sesión
   con acceso al dashboard de Meta.

---

## 5. Rollback del DNS

Si el paso 3 falla, o si tras activar aparece cualquier problema (Chambeando
sirviendo tráfico erróneo, degradación de Barberbot, certificado TLS no
emitido, lo que sea):

```
Hostname:  chambeando-v4-webhook.link-credit.com
Revertir a: el registro/túnel de Cloudflare original (el que estaba activo
            antes de este cambio -- confirmar su valor exacto ANTES de
            hacer el cambio del paso 1, para poder revertir con certeza en
            vez de adivinar).
```

**Acción recomendada antes de ejecutar el paso 1**: anotar/exportar el
registro DNS actual exacto de este hostname (tipo de registro, valor,
proxy Cloudflare on/off) para poder restaurarlo byte a byte si hace falta.
Este documento no lo incluye porque esta sesión no tiene acceso para
consultarlo — es un paso manual del operador antes de ejecutar el cambio.

El rollback de DNS **no afecta** nada del lado del VPS (Caddy, Chambeando,
Barberbot siguen exactamente igual) — es puramente un cambio de qué IP
resuelve el hostname. Si el problema resultara ser del lado del servidor
(no del DNS en sí), usar
`chambeando/scripts/rollback_vps_systemd.sh` para revertir el código de
Chambeando a una release anterior, por separado.

---

## Resumen

```
DNS_CHANGE_AUTHORIZED = YES (por el usuario, en conversación)
DNS_CHANGE_EXECUTED   = NO
PUBLIC_HTTPS_VERIFIED = NO (bloqueado por lo anterior)
META_WEBHOOK_CONFIGURED = NO (bloqueado por lo anterior, y fuera del alcance de esta sesión en cualquier caso)
```
