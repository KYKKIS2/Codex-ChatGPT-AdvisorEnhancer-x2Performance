$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
python (Join-Path $Root "test-advisor-concurrency.py")
exit $LASTEXITCODE
