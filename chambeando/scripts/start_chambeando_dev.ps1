<#
Arranque local del backend Chambeando (UN solo uvicorn sirve /telegram y /whatsapp).

Lee los secretos de los .env existentes. NO contiene ningun secreto y nunca
imprime valores, solo estados.

Por que este script existe y por que carga SIEMPRE todo el entorno:
  * 2026-09-23 manana: uvicorn arrancado sin DATABASE_URL -> cayo al default
    del codigo, creo una sqlite VACIA, y todo POST /telegram/webhook dio 500.
  * 2026-09-23 tarde: uvicorn arrancado solo con el entorno de Telegram -> el
    MISMO proceso servia /whatsapp con WHATSAPP_VERIFY_TOKEN y
    WHATSAPP_APP_SECRET en sus defaults sandbox, asi que rechazaba con 403
    tanto el handshake real de Meta como toda firma real.
  Moraleja: hay un solo backend, asi que hay un solo arranque.

Uso:
  .\scripts\start_chambeando_dev.ps1                      # ambos canales
  .\scripts\start_chambeando_dev.ps1 -Channels telegram
  .\scripts\start_chambeando_dev.ps1 -EnableWhatsAppOutbound   # ver nota abajo

WHATSAPP_PROVIDER se fuerza a "sandbox" salvo que se pase
-EnableWhatsAppOutbound, y ese switch se rechaza mientras Meta reporte
can_send_message=BLOCKED. Motivo: con provider=meta cualquier inbound real
dispara un POST a /messages que hoy solo puede fallar con 141008. El inbound,
la firma y el router se validan igual de verdad en sandbox; lo unico que no
sale es la respuesta.
#>
[CmdletBinding()]
param(
    [ValidateSet('telegram','whatsapp','both')][string]$Channels = 'both',
    [switch]$EnableWhatsAppOutbound,
    [switch]$SkipTunnel
)

$ErrorActionPreference = 'Stop'
$Repo = 'C:\Users\luisd\Desktop\finadvisor-pro\chambeando'
$Py   = Join-Path $Repo '.venv\Scripts\python.exe'
$Cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'

$TG = @{ Tunnel = 'chambeando-telegram-webhook'; Base = 'https://chambeando-telegram.link-credit.com'; Log = 'chambeando-telegram-tunnel.log' }
$WA = @{ Tunnel = 'chambeando-webhook';          Base = 'https://chambeando-webhook.link-credit.com';  Log = 'chambeando-webhook-tunnel.log' }
$WantTG = $Channels -in 'telegram','both'
$WantWA = $Channels -in 'whatsapp','both'
$AppId  = '2278373642738035'

function Fail($m) { Write-Host "FAIL: $m" -ForegroundColor Red; exit 1 }
function Ok($m)   { Write-Host $m -ForegroundColor Green }

Set-Location $Repo
if (-not (Test-Path $Py)) { Fail ".venv no encontrado en $Py" }

# --- 1. entorno: SIEMPRE los cuatro archivos, sin imprimir valores ----------
function Import-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { Fail "Falta el archivo de entorno $Path" }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
}
Import-EnvFile "$Repo\backend\.env.dev_local"               # DATABASE_URL, SECRET_KEY, SETTLEMENT_ENCRYPTION_KEY
Import-EnvFile "$Repo\backend\.env.telegram_local.secrets"  # TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
Import-EnvFile "$Repo\backend\.env.whatsapp_local"          # WHATSAPP_PROVIDER, _GRAPH_API_VERSION, _PHONE_NUMBER_ID
Import-EnvFile "$Repo\backend\.env.whatsapp_local.secrets"  # WHATSAPP_VERIFY_TOKEN, _ACCESS_TOKEN, _APP_SECRET
$env:TELEGRAM_PROVIDER = 'telegram'

$required = 'DATABASE_URL','SECRET_KEY','SETTLEMENT_ENCRYPTION_KEY',
            'TELEGRAM_BOT_TOKEN','TELEGRAM_WEBHOOK_SECRET',
            'WHATSAPP_VERIFY_TOKEN','WHATSAPP_APP_SECRET','WHATSAPP_ACCESS_TOKEN','WHATSAPP_PHONE_NUMBER_ID'
foreach ($k in $required) {
    if (-not (Get-Item "Env:$k" -ErrorAction SilentlyContinue).Value) { Fail "$k no esta definida" }
}
foreach ($k in 'WHATSAPP_VERIFY_TOKEN','WHATSAPP_APP_SECRET','TELEGRAM_WEBHOOK_SECRET') {
    if ((Get-Item "Env:$k").Value -like '*never-use-in-production*') { Fail "$k quedo con el default sandbox del codigo" }
}
if ($env:DATABASE_URL -match '^sqlite:///\.') { Fail 'DATABASE_URL es relativa; usar ruta absoluta (ver cabecera)' }

# --- 2. guardia de outbound WhatsApp ---------------------------------------
$WaHealth = $null
if ($WantWA) {
    $v = $env:WHATSAPP_GRAPH_API_VERSION
    $p = $env:WHATSAPP_PHONE_NUMBER_ID
    try {
        $WaHealth = (Invoke-RestMethod "https://graph.facebook.com/$v/${p}?fields=health_status&access_token=$($env:WHATSAPP_ACCESS_TOKEN)" -TimeoutSec 20).health_status
    } catch { Fail 'no pude leer health_status de Meta (token vencido o sin red?)' }
}
if ($EnableWhatsAppOutbound) {
    if ($WaHealth -and $WaHealth.can_send_message -ne 'AVAILABLE') {
        $codes = ($WaHealth.entities | ForEach-Object { $_.errors } | ForEach-Object { $_.error_code }) -join ','
        Fail "-EnableWhatsAppOutbound rechazado: can_send_message=$($WaHealth.can_send_message) (errores $codes)"
    }
} else {
    $env:WHATSAPP_PROVIDER = 'sandbox'
}

# --- 3. config valida -------------------------------------------------------
$expect = if ($EnableWhatsAppOutbound) { 'meta' } else { 'sandbox' }
$probe = "from backend.config import settings; assert settings.TELEGRAM_PROVIDER=='telegram' and settings.TELEGRAM_BOT_TOKEN; assert settings.WHATSAPP_PROVIDER=='$expect'; assert 'never-use-in-production' not in settings.WHATSAPP_APP_SECRET; assert 'never-use-in-production' not in settings.WHATSAPP_VERIFY_TOKEN; print('CONFIG_OK')"
$cfg = & $Py -c $probe 2>&1
if ($cfg -notmatch 'CONFIG_OK') { Fail "config no valida: $cfg" }
Ok "CONFIG OK (whatsapp_provider=$expect)"

# --- 4. migracion en head ---------------------------------------------------
$cur  = (& $Py -m alembic -c backend/alembic.ini current 2>&1) -join ' '
$head = (& $Py -m alembic -c backend/alembic.ini heads   2>&1) -join ' '
$rev  = ([regex]::Match($head, '[0-9a-f]{12}')).Value
if (-not $rev -or $cur -notmatch $rev) { Fail "DB fuera de head. current=[$cur] heads=[$head]" }
Ok "DB OK (rev $rev)"

# --- 5. puerto 8000: liberar SOLO si es nuestro uvicorn ---------------------
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    # OJO: Win32_Process reporta el interprete BASE para un venv de Windows
    # (pyvenv.cfg -> executable), nunca .venv\Scripts\python.exe. Por eso la
    # identidad se comprueba por command line, no por ruta del ejecutable.
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if ($owner -and $owner.CommandLine -like '*uvicorn*backend.main:app*') {
        Stop-Process -Id $owner.ProcessId -Force
        Start-Sleep -Milliseconds 800
        Write-Host 'PORT 8000 liberado (uvicorn previo)' -ForegroundColor Yellow
    } else {
        Fail "Puerto 8000 ocupado por un proceso ajeno (PID $($listener.OwningProcess)); no lo mato"
    }
}

# --- 6. backend -------------------------------------------------------------
Start-Process -FilePath $Py -WorkingDirectory $Repo -WindowStyle Hidden `
    -ArgumentList '-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000','--log-level','debug' `
    -RedirectStandardOutput "$Repo\backend\.uvicorn_manual.log" `
    -RedirectStandardError  "$Repo\backend\.uvicorn_manual.err.log" | Out-Null

$up = $false
foreach ($i in 1..30) {
    Start-Sleep -Milliseconds 700
    try { if ((Invoke-WebRequest 'http://127.0.0.1:8000/docs' -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { $up = $true; break } } catch {}
}
if (-not $up) { Fail 'backend no respondio en /docs. Ver backend\.uvicorn_manual.err.log' }
Ok 'BACKEND OK'

# --- 7. tuneles -------------------------------------------------------------
function Start-Tunnel($t) {
    $running = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
               Where-Object { $_.CommandLine -like "*$($t.Tunnel)*" }
    if (-not $running) {
        $cfgPath = Join-Path $env:USERPROFILE ".cloudflared\$($t.Tunnel).yml"
        if (-not (Test-Path $cfgPath)) { Fail "Falta la config del tunnel $cfgPath" }
        Start-Process -FilePath $Cloudflared -WindowStyle Hidden `
            -ArgumentList 'tunnel','--config',$cfgPath,'run',$t.Tunnel `
            -RedirectStandardError "$Repo\backend\$($t.Log)" | Out-Null
        Start-Sleep -Seconds 6
    }
    foreach ($i in 1..20) {
        try { if ((Invoke-WebRequest "$($t.Base)/docs" -UseBasicParsing -TimeoutSec 8).StatusCode -eq 200) { return $true } } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}
if (-not $SkipTunnel) {
    if ($WantTG) { if (Start-Tunnel $TG) { Ok 'TUNNEL OK (telegram)' } else { Fail 'tunnel telegram: /docs publico no da 200' } }
    if ($WantWA) { if (Start-Tunnel $WA) { Ok 'TUNNEL OK (whatsapp)' } else { Fail 'tunnel whatsapp: /docs publico no da 200' } }
}

# --- 8. webhooks (todo read-only, nunca imprime tokens) ---------------------
if ($WantTG) {
    try { $info = (Invoke-RestMethod "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/getWebhookInfo" -TimeoutSec 15).result }
    catch { Fail 'getWebhookInfo no respondio' }
    if ($info.url -ne "$($TG.Base)/telegram/webhook") { Fail "webhook telegram apunta a $($info.url)" }
    Ok "WEBHOOK OK (telegram, pending=$($info.pending_update_count))"
}
if ($WantWA) {
    # El handshake real contra NUESTRO proceso: prueba que el verify token
    # cargado es el de Meta y no el default sandbox.
    $c = 'probe_' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $h = Invoke-WebRequest "$($WA.Base)/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=$($env:WHATSAPP_VERIFY_TOKEN)&hub.challenge=$c" -UseBasicParsing -SkipHttpErrorCheck -TimeoutSec 20
    if ($h.StatusCode -ne 200 -or $h.Content -ne $c) { Fail "handshake whatsapp fallo (status=$($h.StatusCode))" }

    $appTok = $AppId + '|' + $env:WHATSAPP_APP_SECRET
    try { $subs = (Invoke-RestMethod "https://graph.facebook.com/$($env:WHATSAPP_GRAPH_API_VERSION)/$AppId/subscriptions?access_token=$appTok" -TimeoutSec 20).data }
    catch { Fail 'no pude leer las suscripciones de la app' }
    $sub = $subs | Where-Object { $_.object -eq 'whatsapp_business_account' } | Select-Object -First 1
    if (-not $sub -or -not $sub.active) { Fail 'la app no tiene una suscripcion activa a whatsapp_business_account' }
    if ($sub.callback_url -ne "$($WA.Base)/whatsapp/webhook") { Fail "callback_url de Meta es $($sub.callback_url)" }
    if ('messages' -notin ($sub.fields | ForEach-Object { $_.name })) { Fail "la app no esta suscrita al campo messages" }
    Ok 'WEBHOOK OK (whatsapp, callback verificado, campo messages activo)'

    # 138024/138025 son de WhatsApp calling (SIP), no de mensajeria: se ignoran.
    foreach ($b in ($WaHealth.entities | Where-Object { $_.can_send_message -eq 'BLOCKED' })) {
        $e = $b.errors | Where-Object { $_.error_code -notin 138024,138025 } | Select-Object -First 1
        if ($e) { Write-Host "OUTBOUND BLOQUEADO POR META: $($b.entity_type) error $($e.error_code) - $($e.error_description)" -ForegroundColor Yellow }
    }
    if (-not $EnableWhatsAppOutbound) {
        Write-Host 'Outbound desactivado (provider=sandbox). Inbound y firma SI se validan de verdad.' -ForegroundColor Yellow
    }
}
