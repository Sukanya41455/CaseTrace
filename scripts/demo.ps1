param(
    [switch]$Headless,
    [switch]$UseExistingFixture
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$artifact = Join-Path $projectRoot "artifacts\casetrace-demo-validated\capability.json"
$query = Join-Path $projectRoot "examples\queries\posted-new-member.json"
$bindings = Join-Path $projectRoot "config\base.json"
$target = "http://127.0.0.1:8000"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$output = Join-Path $projectRoot "runs\demo-$stamp"
$fixture = $null

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project environment not found. Run the README setup commands first."
}
if (-not (Test-Path -LiteralPath $artifact)) {
    throw "Validated demo artifact not found."
}

Set-Location $projectRoot

try {
    if (-not $UseExistingFixture) {
        $existingResponse = $null
        try {
            $existingResponse = Invoke-WebRequest -Uri $target -UseBasicParsing -TimeoutSec 1
        }
        catch {
            # A connection failure is expected when the demo owns fixture startup.
        }
        if ($null -ne $existingResponse) {
            throw "Port 8000 is already serving HTTP. Use -UseExistingFixture only for the normal CaseTrace fixture."
        }
        $logRoot = Join-Path $projectRoot "runs"
        New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
        $fixture = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", "casetrace.cli", "fixture", "--scenario", "normal", "--port", "8000") `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput (Join-Path $logRoot ".demo-fixture-$stamp.out.log") `
            -RedirectStandardError (Join-Path $logRoot ".demo-fixture-$stamp.err.log") `
            -WindowStyle Hidden `
            -PassThru
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($null -ne $fixture -and $fixture.HasExited) {
            throw "Synthetic bank process exited before it became ready."
        }
        try {
            $response = Invoke-WebRequest -Uri $target -UseBasicParsing -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw "Synthetic bank did not become ready on $target."
    }

    Write-Host "Running CaseTrace against the synthetic bank..."
    $replayArgs = @(
        "-m", "casetrace.cli", "replay",
        $artifact,
        "--target", $target,
        "--bindings", $bindings,
        "--params", $query,
        "--output", $output
    )
    if (-not $Headless) {
        $replayArgs += "--headed"
    }
    & $python @replayArgs
    if ($LASTEXITCODE -ne 0) {
        throw "CaseTrace demo replay failed. Evidence is in $output."
    }
    $result = Get-Content -LiteralPath (Join-Path $output "result.json") -Raw | ConvertFrom-Json
    if ($result.kind -ne "success" -or $result.payment.status -ne "POSTED") {
        throw "Demo did not produce the expected POSTED success. Evidence is in $output."
    }
    $modelCalls = Get-Content -LiteralPath (Join-Path $output "events.jsonl") |
        ForEach-Object { ($_ | ConvertFrom-Json).model_call_count }
    if ($modelCalls | Where-Object { $_ -ne 0 }) {
        throw "Demo replay unexpectedly recorded a model call. Evidence is in $output."
    }
    Write-Host "Demo complete. Evidence: $output"
}
finally {
    if ($null -ne $fixture -and -not $fixture.HasExited) {
        Stop-Process -Id $fixture.Id
        $fixture.WaitForExit()
    }
}
