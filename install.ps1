$ErrorActionPreference = "Stop"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 (Join-Path $PSScriptRoot "bamboo.py") install @args
} else {
    & python (Join-Path $PSScriptRoot "bamboo.py") install @args
}
exit $LASTEXITCODE
