# Assina os executaveis do Emissor de Guias com certificado de Code Signing.
#
# Uso (PowerShell):
#   .\assinar.ps1 -Pfx C:\caminho\code-signing.pfx
#   .\assinar.ps1 -Pfx C:\caminho\code-signing.pfx -Recompilar
#   .\assinar.ps1 -ImpressaoDigital 0123456789ABCDEF...   # certificado/token no store
#
# Ordem: o GNRE.exe e assinado primeiro; com -Recompilar o instalador e gerado
# de novo (embutindo o exe ja assinado) e so entao assinado.
#
# A senha nao deve ficar no historico: sem -Senha o script pergunta.
# Nunca versione o .pfx nem a senha junto com o projeto.

param(
    [string]$Pfx,
    [string]$Senha,
    [string]$ImpressaoDigital,
    [string]$Timestamp = "http://timestamp.digicert.com",
    [switch]$StoreMaquina,
    [switch]$Recompilar
)

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppExe = Join-Path $Raiz "dist\GNRE\GNRE.exe"
$InstaladorExe = Join-Path $Raiz "instalador\dist\Instalar GNRE.exe"
$Descricao = "Emissor de Guias - TRANSREIS"

function Achar-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $raizes = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "$env:ProgramFiles\Windows Kits\10\bin",
        "${env:ProgramFiles(x86)}\Windows Kits\8.1\bin"
    )
    foreach ($raiz in $raizes) {
        if (-not (Test-Path $raiz)) { continue }
        $achado = Get-ChildItem $raiz -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($achado) { return $achado.FullName }
    }
    return $null
}

function Obter-Senha {
    if ($script:SenhaGuardada) { return $script:SenhaGuardada }
    if ($Senha) {
        $script:SenhaGuardada = $Senha
        return $Senha
    }
    $segura = Read-Host "Senha do certificado (.pfx)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($segura)
    try {
        $script:SenhaGuardada = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        return $script:SenhaGuardada
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Assinar-Arquivo {
    param([string]$Arquivo)

    if (-not (Test-Path $Arquivo)) {
        throw "Arquivo nao encontrado: $Arquivo"
    }

    $argumentos = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $Timestamp, "/d", $Descricao)
    if ($ImpressaoDigital) {
        $argumentos += @("/sha1", $ImpressaoDigital)
        if ($StoreMaquina) { $argumentos += "/sm" }
    }
    else {
        $argumentos += @("/f", $Pfx, "/p", (Obter-Senha))
    }
    $argumentos += $Arquivo

    Write-Host "Assinando: $Arquivo" -ForegroundColor Cyan
    & $script:SignTool @argumentos
    if ($LASTEXITCODE -ne 0) {
        throw "signtool sign falhou ($LASTEXITCODE) em $Arquivo"
    }
}

function Verificar-Assinatura {
    param([string]$Arquivo)

    Write-Host "Verificando: $Arquivo" -ForegroundColor Cyan
    & $script:SignTool verify /pa /all $Arquivo
    if ($LASTEXITCODE -ne 0) {
        throw "Assinatura invalida em $Arquivo"
    }
}

# -- inicio ---------------------------------------------------------------

if (-not $Pfx -and -not $ImpressaoDigital) {
    Write-Host "Informe -Pfx <arquivo.pfx> ou -ImpressaoDigital <thumbprint>." -ForegroundColor Yellow
    exit 1
}
if ($Pfx -and -not (Test-Path $Pfx)) {
    Write-Host "PFX nao encontrado: $Pfx" -ForegroundColor Red
    exit 1
}

$script:SignTool = Achar-SignTool
if (-not $script:SignTool) {
    Write-Host "signtool.exe nao encontrado. Instale o Windows SDK (Signing Tools) e rode de novo." -ForegroundColor Red
    exit 1
}
Write-Host "signtool: $script:SignTool" -ForegroundColor DarkGray

try {
    Assinar-Arquivo $AppExe

    if ($Recompilar) {
        Write-Host "Recompilando instalador com o exe assinado..." -ForegroundColor Cyan
        Push-Location (Join-Path $Raiz "instalador")
        try {
            & python -m PyInstaller instalador.spec --noconfirm --clean
            if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou ($LASTEXITCODE)" }
        }
        finally {
            Pop-Location
        }
    }
    elseif ((Test-Path $InstaladorExe) -and
            ((Get-Item $InstaladorExe).LastWriteTime -lt (Get-Item $AppExe).LastWriteTime)) {
        Write-Host "ATENCAO: o instalador e mais antigo que o exe assinado; o exe embutido pode estar sem assinatura." -ForegroundColor Yellow
        Write-Host "         Rode com -Recompilar para gerar e assinar o instalador de novo." -ForegroundColor Yellow
    }

    if (Test-Path $InstaladorExe) { Assinar-Arquivo $InstaladorExe }

    Verificar-Assinatura $AppExe
    if (Test-Path $InstaladorExe) { Verificar-Assinatura $InstaladorExe }

    Write-Host ""
    Write-Host "Assinatura concluida." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "FALHOU: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
