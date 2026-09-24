# Chambeando — Server Deployment Runbook (VPS existente de DELIVERYLINK)

Específico para desplegar Chambeando en el VPS existente donde ya corre
**Barberbot**, sobre `bot.link-credit.com`. Complementa (no reemplaza)
`chambeando/DEPLOYMENT_RUNBOOK.md`, que cubre el artefacto Docker
provider-neutral en sí — este documento cubre la integración específica en
ESE servidor.

**Nota de procedencia de este documento**: esta sesión de Claude Code Cloud
nunca tuvo ni tiene acceso al VPS. Todo lo marcado `VERIFIED` en este
documento es lo que el operador (Luis) reportó haber ejecutado y confirmado
directamente en el servidor, fuera de esta sesión — no algo que esta sesión
haya comprobado por sí misma. `PROPOSED` es diseño/recomendación de esta
sesión, aún sin ejecutar. `PENDING` es un paso explícitamente NO ejecutado
todavía, esperando autorización. Esta sesión solo edita archivos dentro del
repositorio; ningún comando de este documento se corrió desde aquí contra el
VPS real.

---

## Estado actual (resumen)

```
SERVER_AUDITED            = VERIFIED (reportado por el operador)
BARBERBOT_DETAILS_KNOWN   = VERIFIED
DEPLOYMENT_PATH_CHOSEN    = VERIFIED — Option B (systemd + venv)
CHAMBEANDO_DEPLOYED_INTERNAL = VERIFIED — 127.0.0.1:8100, health PASS
STABILITY_TEST            = VERIFIED — 5 min, 0 restarts, ~63MB RSS estable
CADDY_BLOCK_ADDED         = VERIFIED — chambeando-v4-webhook.link-credit.com -> 127.0.0.1:8100
DATABASE_DECISION         = VERIFIED (para este piloto) — SQLite persistente bajo /var/lib/chambeando/
DOCKER_BUILD_REAL         = NOT_APPLICABLE_CURRENT_PATH — Docker no está instalado en el VPS y no se usó (se eligió Option B); ver sección 6
DNS_PUBLIC_ACTIVATION     = PENDING — ver chambeando/PUBLIC_ACTIVATION_CHECKLIST.md
META_WEBHOOK_CONFIGURED   = PENDING — bloqueado detrás de DNS_PUBLIC_ACTIVATION
```

---

## EXISTING SERVICE vs NEW SERVICE

```
EXISTING SERVICE: Barberbot     — producción, NO TOUCH
NEW SERVICE:       Chambeando    — desplegado internamente, aislado de Barberbot
```

**Regla dura para todo este documento**: ningún comando, decisión o valor
aquí puede reiniciar, modificar, ni siquiera rozar la configuración de
Barberbot.

---

## 1. Auditoría del VPS — VERIFIED

Resultado reportado por el operador (ejecutado directamente en el VPS, fuera
de esta sesión):

```
VPS IP              = 159.89.231.213
OS                  = Ubuntu 24.04.4 LTS
CPU                 = 1 vCPU
RAM                 = ~961 MiB total
Swap                = ninguno (0)
Disco libre         = ~18 GB
Reverse proxy       = Caddy 2.11.4
Docker              = NO instalado
PostgreSQL          = NO instalado
Python              = 3.12.3, venv disponible
```

El bloque de comandos de auditoría (solo lectura) que produjo estos
resultados queda documentado abajo — sirve para re-auditar en el futuro (ej.
tras cambios de hardware, o antes de un segundo servicio nuevo), no hace
falta volver a correrlo para lo ya confirmado arriba.

```bash
# --- Identidad del sistema ---
echo "== hostname ==" && hostname -f 2>/dev/null || hostname
echo "== OS / version ==" && cat /etc/os-release
echo "== kernel ==" && uname -a
echo "== arquitectura CPU ==" && uname -m && lscpu | grep -E "Architecture|Model name|CPU\(s\)"
echo "== uptime ==" && uptime

# --- Recursos ---
echo "== RAM total/libre ==" && free -h
echo "== swap ==" && swapon --show; cat /proc/swaps
echo "== disco ==" && df -h
echo "== filesystem raiz ==" && findmnt / -o SOURCE,FSTYPE,SIZE,USED,AVAIL

# --- Docker ---
echo "== docker instalado? ==" && command -v docker && docker --version || echo "docker NO encontrado"
echo "== docker daemon activo? ==" && docker info 2>&1 | head -20
echo "== containers existentes ==" && docker ps -a 2>/dev/null
echo "== imagenes existentes ==" && docker images 2>/dev/null
echo "== docker compose? ==" && command -v docker-compose && docker-compose --version; docker compose version 2>/dev/null

# --- systemd / procesos ---
echo "== servicios systemd activos (filtrado por nombres probables) ==" \
  && systemctl list-units --type=service --state=running | grep -iE "bot|barber|chambeando|python|uvicorn|gunicorn|node|docker"
echo "== todos los servicios habilitados (para revision manual) ==" && systemctl list-unit-files --type=service --state=enabled
echo "== procesos relevantes ==" && ps aux | grep -iE "bot|barber|python|uvicorn|gunicorn|node|postgres|nginx|caddy|apache" | grep -v grep

# --- Red / puertos ---
echo "== puertos escuchando ==" && ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null

# --- Firewall (solo lectura) ---
echo "== ufw status ==" && ufw status verbose 2>/dev/null
echo "== iptables (lectura) ==" && iptables -L -n -v 2>/dev/null
echo "== firewalld (si aplica) ==" && firewall-cmd --state 2>/dev/null && firewall-cmd --list-all 2>/dev/null

# --- Reverse proxy: Caddy / Nginx / Apache ---
echo "== caddy instalado? ==" && command -v caddy && caddy version
echo "== caddy service status ==" && systemctl status caddy --no-pager 2>/dev/null
echo "== caddy config activa ==" && cat /etc/caddy/Caddyfile 2>/dev/null; ls -la /etc/caddy/ 2>/dev/null
echo "== nginx instalado? ==" && command -v nginx && nginx -v
echo "== nginx sites activos ==" && ls -la /etc/nginx/sites-enabled/ 2>/dev/null; nginx -T 2>/dev/null | head -100
echo "== apache instalado? ==" && command -v apache2 httpd 2>/dev/null

# --- bot.link-credit.com especificamente ---
echo "== config activa para bot.link-credit.com (grep en caddy/nginx) ==" \
  && grep -rl "bot.link-credit.com" /etc/caddy/ /etc/nginx/ 2>/dev/null \
  && grep -rA 10 "bot.link-credit.com" /etc/caddy/ /etc/nginx/ 2>/dev/null

# --- TLS ---
echo "== certificados TLS conocidos (Caddy autogestiona, o certbot) ==" \
  && command -v certbot && certbot certificates 2>/dev/null
echo "== certs bajo caddy data dir (si aplica, solo listado) ==" \
  && find /var/lib/caddy -iname "*.crt" -o -iname "*.pem" 2>/dev/null | head -20
echo "== inspeccion del cert servido actualmente (no modifica nada) ==" \
  && echo | openssl s_client -connect bot.link-credit.com:443 -servername bot.link-credit.com 2>/dev/null | openssl x509 -noout -dates -subject 2>/dev/null

# --- Barberbot especificamente ---
echo "== ubicacion probable de Barberbot (ajustar patron segun lo que aparezca arriba) ==" \
  && find / -maxdepth 4 -iname "*barber*" -not -path "*/proc/*" 2>/dev/null
echo "== usuario dueño de esos directorios/procesos ==" \
  && find / -maxdepth 4 -iname "*barber*" -not -path "*/proc/*" -exec ls -ld {} \; 2>/dev/null
echo "== puerto interno de Barberbot (cruzar con 'puertos escuchando' + el PID del proceso barber*) ==" \
  && ps aux | grep -i barber | grep -v grep

# --- PostgreSQL ---
echo "== postgres instalado? ==" && command -v psql && psql --version
echo "== servicio postgres ==" && systemctl status postgresql --no-pager 2>/dev/null
echo "== version del servidor (requiere poder conectar, ideal con un usuario read-only) ==" \
  && sudo -u postgres psql -c "SELECT version();" 2>/dev/null
echo "== bases existentes (SOLO nombres/metadatos, CERO datos) ==" \
  && sudo -u postgres psql -c "\l" 2>/dev/null
echo "== tamano de cada base (metadata, no contenido) ==" \
  && sudo -u postgres psql -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database;" 2>/dev/null

# --- Resumen de recursos disponibles ---
echo "== nucleos disponibles ==" && nproc
echo "== carga actual ==" && cat /proc/loadavg
echo "== resumen final (para pegar en el reporte) ==" \
  && echo "RAM: $(free -h | awk '/Mem:/{print $2, "total,", $7, "disponible"}')" \
  && echo "Disco raiz: $(df -h / | awk 'NR==2{print $4, "libres de", $2}')"
```

---

## 2. Plan de aislamiento — VERIFIED (implementado)

```
Barberbot:
  NO TOUCH — confirmado: /root/barberbot/server.js, puerto 3001, Caddy
  `reverse_proxy localhost:3001`. Ningún archivo/proceso/puerto/config de
  Barberbot fue leído para modificar en este deployment, solo para
  verificar ausencia de colisión.

Chambeando (VERIFIED, valores reales ya en uso):
  Directorio release:   /opt/chambeando/releases/<SHA>
  Symlink activo:        /opt/chambeando/current -> releases/d286190d726a956be90df5bbf66e211bb5ff3685
  venv:                   /opt/chambeando/venv (compartido entre releases)
  Datos persistentes:      /var/lib/chambeando/ (SQLite ahí dentro)
  Proceso/servicio:        systemd -- chambeando.service
  Usuario/aislamiento:     ver NOTA abajo -- confirmar con el operador el usuario exacto configurado en la unit (no se nos reportó explícitamente distinto de root; systemd unit debe usar User=/Group= dedicados, nunca root ni el usuario de Barberbot -- ver checklist en PUBLIC_ACTIVATION_CHECKLIST.md antes de exponer públicamente si esto no está ya así)
  Puerto interno:          127.0.0.1:8100 (localhost-only, confirmado != 3001 de Barberbot)
  Env/secrets:              /etc/chambeando/chambeando.env, fuera del repo y fuera del release, EnvironmentFile protegido
  Logs:                     journal propio del unit (`journalctl -u chambeando`), independiente de Barberbot
  Restart policy:           systemd unit propio (`chambeando.service`) -- un fallo de Chambeando no reinicia ni afecta el unit/proceso de Barberbot
  Hostname/subdominio:      chambeando-v4-webhook.link-credit.com (Caddy block ya agregado -- DNS pública aún NO apunta ahí, ver sección "Estado actual")
  Reverse proxy:            bloque Caddy independiente, nuevo, sin editar el bloque existente de Barberbot
  Health check:             GET /health -- VERIFIED 200 {"status":"ok","app":"chambeando-backend"} vía http://127.0.0.1:8100/health
```

---

## 3. Deployment path — VERIFIED: Option B (systemd + venv)

Docker no está instalado en el VPS (confirmado en la auditoría) — se optó
por Option B directamente, sin ambigüedad. Option A (Docker) queda
documentada en `chambeando/DEPLOYMENT_RUNBOOK.md` como artefacto
provider-neutral disponible si el VPS alguna vez incorpora Docker, pero
**no es el camino en uso hoy**.

### OPTION B — systemd + venv (EN USO)

- Virtualenv independiente: `/opt/chambeando/venv` (compartido entre
  releases sucesivos -- cada deploy reinstala `backend/requirements.txt` de
  la release nueva sobre el mismo venv).
- systemd unit: `/etc/systemd/system/chambeando.service` (ver plantilla
  exacta generada por `chambeando/scripts/deploy_vps_systemd.sh`).
- `EnvironmentFile=/etc/chambeando/chambeando.env` -- fuera del repo, nunca
  generado ni sobrescrito por el script de deploy (ver sección 8/FASE 2).
- Comando de producción: `uvicorn backend.main:app --host 127.0.0.1 --port 8100 --no-server-header`
  (host fijo a loopback, nunca `0.0.0.0`, para que la única vía de entrada
  pública sea el bloque Caddy).
- Restart policy: `Restart=on-failure` a nivel systemd, unit propio,
  independiente del de Barberbot.
- Health check: `GET /health` -- ya verificado 200 internamente.
- Reverse proxy: bloque Caddy nuevo, ya agregado y validado (ver más abajo).
- Rollback: `chambeando/scripts/rollback_vps_systemd.sh <SHA-anterior>`.

### OPTION A — Docker (no usada, documentada por completitud)

Ver `chambeando/DEPLOYMENT_RUNBOOK.md` para el artefacto Docker completo
(`Dockerfile`, `.dockerignore`, build/run/rollback). Queda disponible si en
el futuro se instala Docker en este VPS o se migra a otro host.

---

## 4. Database decision — VERIFIED (para este piloto): SQLite persistente

```
DATABASE_DECISION = SQLITE_PERSISTENT_FOR_PILOT (VERIFIED en uso)
```

PostgreSQL **no está instalado** en el VPS (confirmado en la auditoría), así
que el criterio A (preferir Postgres si ya existe y puede aislarse) no
aplicaba. Se optó por la rama B del criterio original: SQLite persistente
bajo `/var/lib/chambeando/` (fuera del árbol del repo/release, sobrevive
redeploys de código), documentado explícitamente como **decisión de piloto,
no permanente** -- ver el análisis completo en la sección 8 (FASE 6) sobre
cuándo esto deja de ser aceptable y Postgres se vuelve obligatorio.

Esto es distinto y deliberadamente mejor que el riesgo original documentado
en `DEPLOYMENT_RUNBOOK.md` sección F (`DATABASE_URL` sin configurar cayendo
al SQLite *no persistente* `./chambeando_v2.db` relativo al cwd del
proceso, que un filesystem efímero borraría en cada restart) -- aquí
`DATABASE_URL` está explícitamente seteado en `/etc/chambeando/chambeando.env`
apuntando a un path fijo y persistente en `/var/lib/chambeando/`, y el VPS
tiene filesystem persistente (no es un container efímero), así que ese
riesgo específico no aplica en este deployment tal como está.

**No se crea ninguna base de datos nueva ni se ejecuta ninguna migración
contra el VPS en este turno** -- la migración ya reportada como parte del
deployment inicial no se repite aquí.

---

## 5. Barberbot safety checklist — VERIFIED

```
BARBERBOT_PROCESS               = /root/barberbot/server.js
BARBERBOT_PORT                  = 3001
BARBERBOT_REVERSE_PROXY         = Caddy 2.11.4 -- reverse_proxy localhost:3001 (bot.link-credit.com)
BARBERBOT_SERVICE_OR_CONTAINER  = proceso Node bajo /root/barberbot (mecanismo exacto de supervisión -- systemd unit propio, pm2, screen/tmux -- no reportado explícitamente; los scripts de esta iteración verifican por PUERTO 3001 en LISTEN, no asumen un nombre de unit systemd para Barberbot, precisamente porque ese detalle no está confirmado)
BARBERBOT_DIRECTORY             = /root/barberbot
```

**Regla explícita, sin excepciones (sigue vigente):**

```
IF ANY BARBERBOT DETAIL IS UNKNOWN:
    DO NOT DEPLOY.
```

Con los 5 datos ya conocidos, el deployment de Chambeando procedió. Los
scripts de deploy/rollback/verify (`chambeando/scripts/*.sh`) verifican en
cada corrida que el puerto 3001 sigue en LISTEN antes y después de tocar
Chambeando -- nunca asumen un nombre de servicio/unit de Barberbot que no
fue confirmado.

**Nota abierta**: el mecanismo exacto de supervisión de Barberbot (systemd /
pm2 / otro) no quedó explícito en el reporte del operador. Si en el futuro
se necesita reiniciar o actualizar Barberbot desde un script, ese dato debe
confirmarse primero -- no se infiere.

---

## 6. Docker build real

```
DOCKER_BUILD_REAL = NOT_APPLICABLE_CURRENT_PATH
```

Docker no está instalado en este VPS y el deployment en uso es Option B
(systemd + venv) -- el artefacto Docker (`chambeando/Dockerfile`) no se
construyó ni se necesitó para este deployment. Sigue disponible y validado
estáticamente (ver `DEPLOYMENT_RUNBOOK.md` sección 6/9) para el día que se
quiera evaluar Docker en este mismo VPS o en otro host. Nada de esto
determina un rollback de código, que en Option B usa
`chambeando/scripts/rollback_vps_systemd.sh`, no imágenes Docker.

---

## 7. Caddy — VERIFIED (bloque agregado y validado por el operador)

```caddyfile
chambeando-v4-webhook.link-credit.com {
    reverse_proxy 127.0.0.1:8100
}
```

Reportado por el operador: `caddy validate` = PASS, `caddy reload` = PASS,
y tras el reload: Caddy sigue activo, Barberbot sigue escuchando en 3001,
Chambeando sigue respondiendo internamente en 127.0.0.1:8100. El bloque de
Barberbot no fue tocado.

Esta sesión **no generó ni aplicó** ese bloque -- se documenta aquí como
hecho ya confirmado por el operador, y los scripts de esta iteración
(sección 8) explícitamente **no modifican Caddy**, solo lo verifican (activo
sí/no) como parte de sus chequeos de seguridad.

**Lo que falta y sigue pendiente**: `chambeando-v4-webhook.link-credit.com`
todavía resuelve al túnel de Cloudflare antiguo (HTTP 530 público). El
bloque Caddy de arriba no sirve tráfico real hasta que el DNS del
subdominio apunte al VPS. Ver `chambeando/PUBLIC_ACTIVATION_CHECKLIST.md`
-- **no ejecutado, pendiente de autorización explícita**.

---

## 8. Scripts de deployment (esta iteración)

Tres scripts nuevos en `chambeando/scripts/`, todos pensados para correr EN
el VPS (no desde esta sesión, que no tiene acceso a él):

- **`deploy_vps_systemd.sh RELEASE_SHA [SOURCE_REPO_DIR]`** -- convierte el
  proceso manual ya validado en un flujo reproducible: preflight (Caddy
  activo, puerto 3001 de Barberbot en LISTEN, puerto 8100 libre o ya
  perteneciente a Chambeando), release directory, venv compartido,
  `pip install -r backend/requirements.txt`, migración Alembic, symlink
  `current` atómico, unit systemd, verificación de salud, verificación de
  seguridad de Barberbot/Caddy post-deploy. **Nunca** genera, imprime, ni
  sobrescribe `/etc/chambeando/chambeando.env` -- si no existe, falla con un
  mensaje claro en vez de inventar secretos. **Nunca** toca Caddy ni DNS.
- **`rollback_vps_systemd.sh RELEASE_SHA_ANTERIOR`** -- mueve el symlink
  `current` atómicamente a una release previa ya presente en
  `/opt/chambeando/releases/`, reinicia solo `chambeando.service`, verifica
  salud + Barberbot + Caddy. No toca env ni DB -- documenta explícitamente
  que un rollback de código NO revierte migraciones de base de datos ya
  aplicadas.
- **`verify_vps_systemd.sh`** -- de solo lectura. Confirma estado del
  servicio, salud, binding, memoria/reinicios, Caddy, Barberbot, disco,
  release activa, existencia de la DB SQLite y permisos básicos, sin
  imprimir secretos. Termina con `VERIFY_GATE=PASS/FAIL`.

Ninguno de los tres se ejecutó contra el VPS real desde esta sesión --
solo se validó su sintaxis con `bash -n` (ver reporte final).

---

## 9. FASE 6 — Revisión técnica de SQLite para este piloto

**Contexto real**: Chambeando corre como **un único proceso Uvicorn**
(systemd `Type=simple`, sin múltiples workers), sirviendo un volumen de
tráfico de piloto (WhatsApp/Telegram en modo sandbox todavía, sin usuarios
reales de producción activos). Esto cambia sustancialmentre el perfil de
riesgo de SQLite respecto a un deployment con varios workers/réplicas.

### Journal mode actual esperado

El código (`backend/database.py`) no fija explícitamente ningún
`PRAGMA journal_mode` -- SQLAlchemy/SQLite usan el default de SQLite, que es
`DELETE` (rollback journal clásico, no WAL). Esto es lo que corre hoy en
`/var/lib/chambeando/`. No se ha verificado el modo activo real en el VPS
(requeriría `PRAGMA journal_mode;` contra el archivo -- no ejecutado en esta
sesión, sin acceso al VPS).

### Concurrency / locking risks

- SQLite con journal `DELETE` (el default) usa locking a nivel de **todo el
  archivo de base de datos**: un writer bloquea a todos los demás
  writers/readers mientras dura la transacción de escritura.
- Con **un solo proceso Uvicorn** (el caso actual), las escrituras ya están
  serializadas por el GIL + el hecho de ser un único proceso -- el riesgo de
  contención real es bajo comparado con múltiples workers/procesos
  concurrentes escribiendo a la vez.
- El riesgo real hoy no es tanto "dos requests chocan" sino: una request de
  escritura lenta (ej. una transacción larga) bloquea brevemente a otras
  requests concurrentes de lectura/escritura dentro del mismo proceso,
  aumentando latencia bajo carga -- no corrupción, solo contención.
- Si en el futuro se escala a **más de un worker Uvicorn** (`--workers N`)
  o a múltiples réplicas del servicio, el riesgo de locking sube
  significativamente y SQLite deja de ser una buena elección -- ver
  "Postgres migration trigger" abajo.

### Backup requirements

- **No hay backup automatizado confirmado hoy** para
  `/var/lib/chambeando/*.db` -- no fue parte de lo reportado por el
  operador. Esto es una brecha real para un piloto que empiece a acumular
  datos reales (invites, órdenes, disputas).
- Recomendación mínima (no implementada en esta sesión): un cron simple que
  copie el archivo `.db` a una ruta separada (o a almacenamiento fuera del
  VPS) con `sqlite3 <db> ".backup '<destino>'"` -- el comando `.backup`
  usa la API de backup online de SQLite, segura de correr con la DB en uso,
  a diferencia de un `cp` directo del archivo mientras el proceso escribe.
- Fuera del alcance de esta iteración (no pedido explícitamente) -- se deja
  como recomendación, no como script nuevo.

### Riesgo de un solo proceso Uvicorn

Bajo para el piloto actual: sin múltiples workers no hay condición de
carrera entre procesos distintos sobre el mismo archivo SQLite. El riesgo
principal no es de integridad de datos sino de **disponibilidad**: si el
único proceso crashea, systemd lo reinicia (`Restart=on-failure`), pero hay
una ventana de downtime real (sin réplica). Esto es un tradeoff aceptado
implícitamente al elegir Option B de proceso único para el piloto, no algo
nuevo introducido por SQLite en sí.

### WAL mode — evidencia, pros/contras (recomendación, NO implementada)

**Recomendación**: activar `PRAGMA journal_mode=WAL;` sería una mejora
técnicamente segura y de bajo riesgo para este piloto específico, PERO
**no se implementa en esta sesión** -- ver justificación abajo.

- **Pros**: WAL permite que lectores concurrentes no bloqueen a un writer
  (y viceversa, salvo el caso muy raro de checkpoint), reduce la latencia
  bajo el patrón típico de esta app (muchas lecturas de estado + escrituras
  puntuales de eventos/órdenes), y es la recomendación estándar de SQLite
  para cualquier app servida por web (no solo scripts de un solo uso). Es
  ampliamente considerado más robusto para procesos de larga duración que
  el journal mode `DELETE` por defecto.
- **Contras / riesgos a considerar**: WAL requiere que el filesystem
  soporte locking compartido correctamente (el VPS con ext4/Ubuntu estándar
  lo soporta sin problema, no es un riesgo real aquí); genera archivos
  adicionales (`-wal`, `-shm`) junto al `.db` principal, que un backup
  naive (un `cp` del `.db` solo, sin los otros dos) podría dejar
  inconsistente -- razón de más para usar `sqlite3 .backup` en vez de `cp`
  crudo, como se recomienda arriba independientemente del journal mode.
- **Por qué no lo activo ahora**: el código actual (`backend/database.py`)
  no tiene ningún test que ejercite el comportamiento bajo WAL
  específicamente, y activar un PRAGMA de journal mode es un cambio de
  comportamiento de infraestructura que toca el archivo de datos ya en
  producción del piloto -- exactamente el tipo de cambio que las
  instrucciones de esta tarea piden no aplicar salvo que sea "inequívocamente
  seguro y tenga tests". Cambiarlo requeriría, como mínimo: un test que
  confirme que `create_all`/Alembic siguen funcionando igual bajo WAL, y
  aplicarlo contra el archivo real del VPS (fuera del alcance de una sesión
  sin acceso a él). Queda como recomendación clara para una iteración futura
  y explícita, no aplicada aquí.

### Cuándo PostgreSQL se vuelve obligatorio (Postgres migration trigger)

Cualquiera de estas condiciones, no implementado --> migrar antes de
cruzarla, no después:

1. **Más de un worker/proceso Uvicorn**, o cualquier forma de
   escalado horizontal (más de una instancia del servicio).
2. **WhatsApp o Telegram salen de modo sandbox** hacia tráfico real de
   usuarios -- el volumen y la concurrencia de webhooks entrantes deja de
   ser predecible.
3. Necesidad real de **alta disponibilidad** (failover automático) -- SQLite
   en un solo archivo en un solo VPS no lo ofrece por diseño.
4. El tamaño de la base o la tasa de escritura empieza a mostrar latencia
   perceptible en producción (señal empírica, a monitorear con
   `verify_vps_systemd.sh`, no una fecha fija).

---

## 10. FASE 7 — Riesgo de recursos (RAM/swap)

**Contexto real**: 961 MiB RAM total, 0 swap, 1 vCPU, compartido con
Barberbot y `tasas-cuba-bot-baileys` en el mismo VPS. Chambeando reportó
~63 MB RSS estable en la prueba de 5 minutos.

### Análisis del riesgo

- **RAM_RISK = MEDIUM.** No es HIGH porque el footprint medido de
  Chambeando (~63MB) es pequeño frente al total (~961MB) y la prueba de
  estabilidad no mostró crecimiento (sin indicios de memory leak en 5
  minutos -- una ventana corta, pero sin señales negativas). No es LOW
  porque: (a) sin swap, cualquier pico de memoria que exceda el
  RAM físico dispara el OOM killer del kernel de inmediato, sin margen de
  degradación gradual; (b) el VPS ya corre **tres** procesos de
  aplicación (Barberbot, tasas-cuba-bot-baileys, y ahora Chambeando) más
  Caddy sobre un total de ~961MB -- el margen combinado es estrecho, y un
  pico simultáneo de los tres (ej. bajo carga real de WhatsApp/Telegram una
  vez salgan de sandbox) es una situación no probada todavía.
- El OOM killer del kernel, si dispara, puede matar **cualquier** proceso
  del sistema según su heurística (no necesariamente el que causó el pico)
  -- esto incluye el riesgo teórico de que un pico de memoria de Chambeando
  cause que el kernel mate a Barberbot en vez de a sí mismo. Es un riesgo
  real de "vecino ruidoso" en un VPS de 1 vCPU/~1GB compartido por 3+
  servicios.

### Límite seguro propuesto para el systemd unit (PROPUESTO, NO aplicado)

Con ~63MB medido en reposo/piloto, un `MemoryMax=` de **256M** en la unit de
Chambeando sería un límite conservador y razonable: deja ~4x margen sobre lo
medido para picos normales de tráfico, sin arriesgar acaparar una porción
desproporcionada de los ~961MB totales que también necesitan Barberbot,
tasas-cuba-bot-baileys, Caddy y el propio sistema operativo. `MemoryHigh=`
en, por ejemplo, **192M** (soft throttle antes de llegar al hard limit)
sería un complemento razonable para degradar antes de que el kernel tenga
que intervenir.

**Esto es análisis y recomendación únicamente.** Por instrucción explícita
de esta iteración, `MemoryMax` NO se cambia ni se aplica desde aquí, y
`deploy_vps_systemd.sh` (sección 8) **no** incluye `MemoryMax=`/`MemoryHigh=`
en la unit que genera -- se deja como paso futuro deliberado y separado,
con su propia autorización.

### Recomendación de swap

**No crear swap en esta sesión** (instrucción explícita). Como análisis: un
swapfile pequeño (ej. 512MB-1GB) sería una mitigación razonable y de bajo
costo contra el escenario "pico corto de memoria mata el proceso
equivocado" descrito arriba -- convierte un OOM-kill duro en degradación de
performance temporal. Contras a considerar antes de aplicarlo: I/O de swap
en un VPS con disco de red puede ser lento y, si el pico es sostenido (no
corto), solo retrasa el problema en vez de resolverlo. Queda como
recomendación para que el operador decida, no como acción de esta
iteración.

---

## 11. Resumen final

```
SERVER_AUDITED             = VERIFIED
BARBERBOT_DETAILS_KNOWN    = VERIFIED
DEPLOYMENT_PATH_CHOSEN      = VERIFIED (Option B)
CHAMBEANDO_INTERNAL_HEALTH  = VERIFIED PASS
STABILITY_TEST              = VERIFIED PASS
DATABASE_DECISION           = SQLITE_PERSISTENT_FOR_PILOT (ver sección 9 para el trigger de migración a Postgres)
DOCKER_BUILD_REAL           = NOT_APPLICABLE_CURRENT_PATH
CADDY_BLOCK                 = VERIFIED (agregado, validado, recargado)
DNS_PUBLIC_ACTIVATION       = PENDING -- ver chambeando/PUBLIC_ACTIVATION_CHECKLIST.md
```

**Próximo paso único:** activación pública vía DNS, siguiendo
`chambeando/PUBLIC_ACTIVATION_CHECKLIST.md` -- no ejecutado, pendiente de
autorización explícita del operador.
