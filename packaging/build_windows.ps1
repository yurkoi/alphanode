# Local Windows build: PyInstaller onedir -> selfcheck -> smoke -> portable zip -> Inno Setup installer.
# Mirrors the windows job in .github/workflows/build.yml. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Needs: Python 3.11/3.12, Visual Studio 2022 Build Tools (C++ x64), Inno Setup 6, internet (vault key).
$ErrorActionPreference = 'Stop'
$Proj = Split-Path -Parent $PSScriptRoot
Set-Location $Proj
if (-not $env:VAULT_API)           { $env:VAULT_API = 'https://api.alphanode.tech' }
if (-not $env:ALPHANODE_VAULT_URL) { $env:ALPHANODE_VAULT_URL = $env:VAULT_API }
$env:ALPHANODE_REQUIRE_NUMBA = '1'

if (-not (Test-Path .venv-build\Scripts\python.exe)) { python -m venv .venv-build }
$py = "$Proj\.venv-build\Scripts\python.exe"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r packaging\requirements-build.txt

# Refresh the vault public key when the server answers; otherwise keep the bundled one
# (a dead api host must not block a local build — selfcheck still proves the key seals).
try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 "$env:VAULT_API/pub.txt" -OutFile alphanode\vault_server_key.pub.new -ErrorAction Stop
      Move-Item -Force alphanode\vault_server_key.pub.new alphanode\vault_server_key.pub }
catch { Remove-Item -Force -ErrorAction SilentlyContinue alphanode\vault_server_key.pub.new
        if (-not (Test-Path alphanode\vault_server_key.pub)) { throw "vault key unreachable and no local copy: $_" }
        Write-Warning "vault key not refreshed ($($_.Exception.Message)) - using local alphanode\vault_server_key.pub" }
& $py packaging\make_icon.py

# Cython needs cl.exe on PATH: run it through vcvars64
$vcvars = Get-ChildItem "C:\Program Files*\Microsoft Visual Studio\2022\*\VC\Auxiliary\Build\vcvars64.bat" | Select-Object -First 1
if (-not $vcvars) { throw 'vcvars64.bat not found - install VS 2022 C++ build tools' }
cmd /c "`"$($vcvars.FullName)`" >nul && `"$py`" packaging\cythonize_engine.py"
if ($LASTEXITCODE -ne 0) { throw 'cythonize failed' }

& $py packaging\make_build_stamp.py
& "$Proj\.venv-build\Scripts\pyinstaller.exe" --noconfirm --clean --distpath packaging\dist --workpath packaging\build packaging\AlphaNode.spec
if ($LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }

$p = Start-Process -FilePath "packaging\dist\AlphaNode\AlphaNode.exe" -ArgumentList '--role','selfcheck' `
     -WorkingDirectory "packaging\dist\AlphaNode" -Wait -PassThru
if (Test-Path packaging\dist\AlphaNode\selfcheck.log) { Get-Content packaging\dist\AlphaNode\selfcheck.log }
if ($p.ExitCode -ne 0) { throw "selfcheck failed ($($p.ExitCode))" }
& $py packaging\ci_smoke.py packaging\dist\AlphaNode\AlphaNode.exe
if ($LASTEXITCODE -ne 0) { throw 'smoke failed' }

Compress-Archive -Force -Path packaging\dist\AlphaNode\* -DestinationPath packaging\dist\AlphaNode-windows-portable.zip
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
& $iscc /Q packaging\AlphaNode.iss
if ($LASTEXITCODE -ne 0) { throw 'ISCC failed' }
Write-Host "`nDONE:`n  packaging\Output\AlphaNode-Setup.exe`n  packaging\dist\AlphaNode-windows-portable.zip"
