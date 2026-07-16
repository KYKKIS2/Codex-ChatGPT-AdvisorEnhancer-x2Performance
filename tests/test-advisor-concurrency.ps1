$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
python (Join-Path $Root "tests\test-advisor-concurrency.py")
exit $LASTEXITCODE
