$ErrorActionPreference = "Stop"

uv sync
if ($LASTEXITCODE -eq 0) {
    exit 0
}

Write-Warning "uv editable build failed; retrying with the Windows PEP 517 fallback."
uv sync --no-install-project
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

uv pip install pip
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& .venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
exit $LASTEXITCODE
