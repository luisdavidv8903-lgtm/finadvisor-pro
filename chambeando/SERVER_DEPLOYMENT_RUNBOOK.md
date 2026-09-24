# Chambeando — Server Deployment Runbook (VPS existente de DELIVERYLINK)

Específico para desplegar Chambeando en el VPS existente donde ya corre
**Barberbot**, sobre `bot.link-credit.com`. Complementa (no reemplaza)
`chambeando/DEPLOYMENT_RUNBOOK.md`, que cubre el artefacto Docker
provider-neutral en sí — este documento cubre la integración específica en
ESE servidor.

**Nada en este documento se ha ejecutado.** No se accedió al VPS, no se
auditó nada, no se desplegó nada, no se tocó Meta, DNS ni Barberbot. Todo lo
de abajo es preparación para cuando decidas ejecutar la auditoría.

---

## EXISTING SERVICE vs NEW SERVICE

```
EXISTING SERVICE: Barberbot           — producción, NO TOUCH
NEW SERVICE:       Chambeando          — a desplegar, aislado de Barberbot
```

**Regla dura para todo este documento**: ningún comando, decisión o valor
aquí puede reiniciar, modificar, ni siquiera rozar la configuración de
Barberbot. Donde no lo sepamos todavía (puerto, proceso, proxy, directorio),
queda como `<TO_BE_DETERMINED_BY_AUDIT>` — nunca se asume.

---

## 1. Auditoría read-only del VPS

Bloque de comandos para ejecutar por SSH, **todos de solo lectura**. Ninguno
reinicia servicios, edita archivos, cambia firewall/DNS, crea usuarios o
bases de datos, ni borra nada. Pensado para correr de un tirón y pegar la
salida completa antes de decidir nada del deployment.

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

**Cómo usar esto**: correr el bloque completo por SSH, pegar toda la salida
en la conversación. A partir de ahí completamos las secciones 3-6 de este
documento con valores reales en vez de placeholders.

---

## 2. Plan de aislamiento (diseño objetivo, sin valores inventados)

```
Barberbot:
  NO TOUCH — ningún archivo, proceso, puerto, DB o config de Barberbot se
  lee para modificar, solo se lee para EVITAR colisión.

Chambeando:
  Directorio:        <TO_BE_SELECTED_AFTER_AUDIT>   (ej. /opt/chambeando o /srv/chambeando -- decidir según convención ya usada por Barberbot, sección 6)
  Proceso/container:  independiente -- ver OPTION A / OPTION B (sección 4)
  Usuario/aislamiento: usuario de sistema dedicado (ej. `chambeando`, sin login shell), NUNCA el usuario que corre Barberbot ni root para el proceso de la app
  Puerto interno:      CHAMBEANDO_INTERNAL_PORT=<TO_BE_SELECTED_AFTER_AUDIT>  -- debe ser distinto de BARBERBOT_PORT (sección 6) y de cualquier otro puerto ya en LISTEN
  Env/secrets:          archivo separado fuera del repo (ej. /etc/chambeando/production.env), permisos 600, dueño = usuario de Chambeando -- nunca compartido con el .env de Barberbot
  Logs:                 independientes -- si Docker, `docker logs chambeando-backend`; si systemd, journal propio del unit (`journalctl -u chambeando`) -- nunca mezclados con el log de Barberbot
  Restart policy:       independiente -- `--restart unless-stopped` (Docker) o systemd unit propio con su propio `Restart=`; un fallo de Chambeando NUNCA debe poder tumbar o reiniciar el unit/container de Barberbot
  Hostname/subdominio:  propio -- ej. un subdominio distinto de bot.link-credit.com (a definir; NO reutilizar el hostname de Barberbot)
  Reverse proxy:        entrada independiente en la config del proxy que ya exista (Caddy/Nginx/Apache -- lo que confirme la auditoría), como bloque/server nuevo, nunca editando el bloque existente de Barberbot
  Health check:          propio -- GET /health de Chambeando (ya implementado), monitoreado por separado del health check de Barberbot si existe
```

No se fija ningún valor concreto (puerto, ruta, dominio) hasta tener los
resultados de la sección 1.

---

## 3. Dos caminos de deployment

La decisión entre A y B se toma **después** de la auditoría (Docker instalado
y con daemon operativo → A; si no, o si el operador prefiere el patrón que ya
usa para Barberbot → B, siempre que se confirme qué usa Barberbot en la
sección 6).

### OPTION A — Docker

- Artefacto: `chambeando/Dockerfile` + `chambeando/.dockerignore` (ya
  creados y validados estáticamente — ver `DEPLOYMENT_RUNBOOK.md`).
- Container independiente, nombre propio (ej. `chambeando-backend`), nunca
  compartiendo red/volumen con el container de Barberbot salvo que la
  auditoría muestre que Barberbot no usa Docker en absoluto (en cuyo caso no
  hay colisión posible por diseño).
- `--env-file` apuntando a un archivo **fuera del repo** en el VPS (ej.
  `/etc/chambeando/production.env`, permisos 600).
- `--restart unless-stopped`.
- `HEALTHCHECK` ya integrado en la imagen (`GET /health`).
- Persistent storage: **ninguno para la app en sí** (stateless) — la única
  necesidad de persistencia real es la base de datos, ver sección 5. Si
  Postgres corre en el mismo VPS fuera de Docker, no hace falta volumen; si
  correrá en un container Postgres separado, ese sí necesita un volumen
  nombrado propio, distinto de cualquier volumen que use Barberbot.
- Reverse proxy: nueva entrada en el proxy existente, apuntando a
  `127.0.0.1:<CHAMBEANDO_INTERNAL_PORT>` publicado por el container.
- Rollback: re-`docker run` con un tag de imagen anterior (`chambeando-backend:<git-sha-anterior>`) — ver `DEPLOYMENT_RUNBOOK.md` sección L.

### OPTION B — systemd + venv

- Virtualenv independiente en el directorio de Chambeando (`<TO_BE_SELECTED_AFTER_AUDIT>/venv`), nunca reutilizando el venv de Barberbot aunque ambos sean Python.
- Usuario/servicio systemd dedicado, ej.:
  ```ini
  [Unit]
  Description=Chambeando backend
  After=network.target

  [Service]
  Type=simple
  User=chambeando
  Group=chambeando
  WorkingDirectory=<TO_BE_SELECTED_AFTER_AUDIT>/chambeando
  EnvironmentFile=/etc/chambeando/production.env
  ExecStart=<TO_BE_SELECTED_AFTER_AUDIT>/venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --no-server-header
  Restart=on-failure
  RestartSec=5
  NoNewPrivileges=true

  [Install]
  WantedBy=multi-user.target
  ```
  (Plantilla — no se ha creado ni instalado ningún unit file real; `<TO_BE_SELECTED_AFTER_AUDIT>` se completa tras la auditoría.)
- `EnvironmentFile` fuera del repo, mismo criterio que Option A (permisos
  600, dueño = usuario de Chambeando).
- Comando de producción: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --no-server-header` (el mismo verificado en `DEPLOYMENT_RUNBOOK.md` — sin `--reload`).
- Restart policy: `Restart=on-failure` a nivel systemd — independiente del
  unit de Barberbot, que sigue con su propia policy intacta.
- Health check: mismo `GET /health`, monitoreado con lo que ya use el VPS
  (cron+curl, Uptime Kuma, etc. — a confirmar en la auditoría si algo así ya
  existe para Barberbot).
- Reverse proxy: igual que Option A, nueva entrada apuntando a
  `127.0.0.1:<CHAMBEANDO_INTERNAL_PORT>`.
- Rollback: `git checkout <sha-anterior>` en el directorio de Chambeando +
  `systemctl restart chambeando` (nunca tocando el unit de Barberbot).

---

## 4. Database decision gate

El reporte técnico anterior encontró que `DATABASE_URL` cae por defecto a
`sqlite:///./chambeando_v2.db` si no se configura explícitamente, lo cual es
un riesgo alto en cualquier filesystem efímero (ver `DEPLOYMENT_RUNBOOK.md`
sección F). En un VPS con filesystem persistente esto es menos grave, pero
sigue sin ser la elección correcta para producción real.

**No se decide la base de datos final hasta auditar el VPS.**

```
DATABASE_DECISION = PENDING_SERVER_AUDIT
```

Criterio a aplicar una vez tengamos los resultados de la sección 1:

- **A. Si PostgreSQL ya está disponible en el VPS y puede aislarse**
  (usuario y base de datos propios para Chambeando, sin tocar las bases de
  Barberbot): preferir esto. `DATABASE_URL` apuntaría a
  `postgresql://<chambeando_db_user>@localhost/<chambeando_db_name>` con
  usuario/base creados específicamente para Chambeando — nunca reusar
  credenciales ni la base de Barberbot.
- **B. Si no existe PostgreSQL en el VPS**: evaluar instalar Postgres
  separado (contenedorizado o nativo, dedicado a Chambeando), o —solo si es
  técnicamente aceptable para la carga inicial esperada y se documenta como
  decisión temporal, no permanente— SQLite persistente en un path fuera del
  árbol del repo/imagen, con backup explícito (ver `DEPLOYMENT_RUNBOOK.md`
  sección L sobre por qué esto no es lo mismo que el fallback silencioso
  actual).

**No se crea ninguna base de datos, usuario de DB, ni se ejecuta ninguna
migración contra el VPS en este turno.**

---

## 5. Barberbot safety checklist

Antes de cualquier deployment futuro, estos datos deben conocerse con
certeza (se completan con la salida de la sección 1):

```
BARBERBOT_PROCESS          = <TO_BE_DETERMINED_BY_AUDIT>
BARBERBOT_PORT              = <TO_BE_DETERMINED_BY_AUDIT>
BARBERBOT_REVERSE_PROXY     = <TO_BE_DETERMINED_BY_AUDIT>   (Caddy / Nginx / Apache / ninguno)
BARBERBOT_SERVICE_OR_CONTAINER = <TO_BE_DETERMINED_BY_AUDIT>  (systemd unit / docker container / otro)
BARBERBOT_DIRECTORY          = <TO_BE_DETERMINED_BY_AUDIT>
```

**Regla explícita, sin excepciones:**

```
IF ANY BARBERBOT DETAIL IS UNKNOWN:
    DO NOT DEPLOY.
```

Ningún valor de `CHAMBEANDO_INTERNAL_PORT`, directorio, nombre de
servicio/container, ni entrada de reverse proxy se selecciona en firme hasta
que las cinco líneas de arriba estén completas y confirmadas como
NO-colisionantes con lo elegido para Chambeando.

---

## 6. Docker build real

Este entorno (Claude Code Cloud) no tiene daemon Docker activo — confirmado
en la iteración anterior (`docker ps` falla con "no such file or directory"
sobre `/var/run/docker.sock`). No se puede resolver desde aquí.

```
DOCKER_BUILD_REAL = PENDING_VPS_OR_LOCAL_DOCKER
```

Antes de cualquier deployment real con Option A, ejecutar en una máquina con
Docker operativo (el VPS mismo, tras confirmar que tiene Docker — sección 1
— o una máquina local):

```bash
cd chambeando
docker build -t chambeando-backend:test -f Dockerfile .
docker run --rm -d --name chambeando-build-test -p 18000:8000 \
  -e SECRET_KEY=build-test-only-not-real \
  -e SETTLEMENT_ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  -e DATABASE_URL=sqlite:////tmp/build-test.db \
  chambeando-backend:test
sleep 3
curl -sf http://127.0.0.1:18000/health && echo " -- /health OK"
docker exec chambeando-build-test sh -c 'ls /app/backend | grep -E "\.env$|\.secrets$"' && echo "ALERTA: secretos encontrados en la imagen" || echo "OK: sin .env/.secrets dentro del container"
docker stop chambeando-build-test
```

Checklist a confirmar con esa corrida:
- [ ] `docker build` termina sin error.
- [ ] El container arranca y sigue corriendo (no crashea al boot).
- [ ] `GET /health` responde 200.
- [ ] Ningún `.env`/`.secrets` real terminó dentro de la imagen (el comando
      de arriba lo confirma por ausencia — también se puede correr `docker
      history` / `docker inspect` para una revisión más exhaustiva).

No ejecutado en este turno — placeholder para cuando haya un Docker real
disponible.

---

## 7. Resumen de estado

```
SERVER_AUDITED           = NO
BARBERBOT_DETAILS_KNOWN  = NO
DEPLOYMENT_PATH_CHOSEN   = NOT_YET (depende de la auditoría, sección 3)
DATABASE_DECISION        = PENDING_SERVER_AUDIT
DOCKER_BUILD_REAL        = PENDING_VPS_OR_LOCAL_DOCKER
DEPLOYED                 = NO
```

**Próximo paso único:** ejecutar el bloque de la sección 1 por SSH contra el
VPS y traer la salida completa. Ningún otro paso de este documento avanza
hasta entonces.
