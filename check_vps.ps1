<#
═══════════════════════════════════════════════════════════════════════════════
 Polybot Snipez — Vultr VPS Health Check
 Run this from your Windows machine to verify the VPS is up and the bot running.

 Usage:
   .\check_vps.ps1 -VpsIp 1.2.3.4
   .\check_vps.ps1 -VpsIp 1.2.3.4 -User root

 What it checks:
   1. Network reachability (ping)
   2. SSH login works
   3. systemd polybot.service is active
   4. Dashboard HTTP port 5050 is responding
═══════════════════════════════════════════════════════════════════════════════
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$VpsIp,

    [string]$User = "root",

    [int]$DashboardPort = 5050
)

$ErrorActionPreference = "Continue"
$ok   = "[ OK ]"
$fail = "[FAIL]"
$allGood = $true

function Write-Step($pass, $label, $detail = "") {
    $tag = if ($pass) { $ok } else { $fail }
    $color = if ($pass) { "Green" } else { "Red" }
    Write-Host ("{0} {1}" -f $tag, $label) -ForegroundColor $color
    if ($detail) { Write-Host ("       {0}" -f $detail) -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "  Checking Polybot VPS at $VpsIp ..." -ForegroundColor Cyan
Write-Host ""

# ── 1. Ping ──────────────────────────────────────────────────────────────────
$ping = Test-Connection -ComputerName $VpsIp -Count 2 -Quiet -ErrorAction SilentlyContinue
Write-Step $ping "Host reachable (ping)"
if (-not $ping) { $allGood = $false }

# ── 2. SSH + 3. systemd service ──────────────────────────────────────────────
# One SSH call grabs both login success and service status.
$sshTarget = "$User@$VpsIp"
$remoteCmd = "systemctl is-active polybot.service 2>/dev/null; echo '---'; systemctl status polybot.service --no-pager -n 3 2>/dev/null | head -n 12"
$sshOut = ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new $sshTarget $remoteCmd 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Step $true "SSH login works ($sshTarget)"
    $serviceState = ($sshOut -split "`n")[0].Trim()
    $serviceUp = ($serviceState -eq "active")
    Write-Step $serviceUp "polybot.service is active" "state: $serviceState"
    if (-not $serviceUp) {
        $allGood = $false
        Write-Host ($sshOut -join "`n") -ForegroundColor DarkGray
    }
} else {
    Write-Step $false "SSH login works ($sshTarget)" "ssh exit $LASTEXITCODE — check key/IP/firewall"
    Write-Host ($sshOut -join "`n") -ForegroundColor DarkGray
    $allGood = $false
}

# ── 4. Dashboard HTTP ────────────────────────────────────────────────────────
$url = "http://${VpsIp}:${DashboardPort}/"
try {
    $resp = Invoke-WebRequest -Uri $url -TimeoutSec 10 -MaximumRedirection 5 -UseBasicParsing
    $httpUp = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    Write-Step $httpUp "Dashboard responding on port $DashboardPort" "HTTP $($resp.StatusCode) — $url"
    if (-not $httpUp) { $allGood = $false }
} catch {
    # A 401/redirect to login still means the server is alive.
    $code = $_.Exception.Response.StatusCode.value__
    if ($code) {
        Write-Step $true "Dashboard responding on port $DashboardPort" "HTTP $code (login/auth) — $url"
    } else {
        Write-Step $false "Dashboard responding on port $DashboardPort" "no response — $($_.Exception.Message)"
        $allGood = $false
    }
}

Write-Host ""
if ($allGood) {
    Write-Host "  ✓ VPS is UP and the bot is running." -ForegroundColor Green
    exit 0
} else {
    Write-Host "  ✗ Something is down — see failures above." -ForegroundColor Red
    Write-Host "    Debug:  ssh $sshTarget 'journalctl -u polybot -n 50 --no-pager'" -ForegroundColor DarkGray
    exit 1
}
