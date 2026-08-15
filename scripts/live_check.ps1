# Live check. Needs $env:ANTHROPIC_API_KEY. ~$0.60 total.
#   .\scripts\live_check.ps1
# Output lands in examples\ and is echoed to the console.

$ErrorActionPreference = "Continue"

# The console codepage decides whether output renders or turns into mojibake.
# The tool emits ASCII so this is belt and braces, not a requirement.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path examples | Out-Null

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host "ANTHROPIC_API_KEY is not set. Run:" -ForegroundColor Yellow
    Write-Host '  $env:ANTHROPIC_API_KEY = "sk-ant-..."'
    exit 1
}

# NOTE: the parameter is not called $Args. That is a PowerShell automatic
# variable, and using it here silently splats nothing -- every call became a
# bare `tracelens ask` with no question.
function Ask {
    param([string]$Slug, [string[]]$CliArgs)
    Write-Host ""
    Write-Host "=== $Slug ===" -ForegroundColor Cyan
    tracelens --plain ask @CliArgs 2>&1 | Tee-Object -FilePath "examples\$Slug.txt"
}

# telemetry quality — the question the provided symptoms never ask
Ask q1-telemetry-correct @("are the email pipeline logs and telemetry correct? can I trust what I'm seeing")
Ask q2-would-we-notice   @("if something broke right now, would our monitoring catch it?")
Ask q3-alerting          @("we're about to build alerting on this pipeline. what should we not build it on?")

# the five provided symptoms
Ask s1-push      @("--symptom", "1")
Ask s2-duplicate @("--symptom", "2")
Ask s3-slow      @("--symptom", "3")
Ask s4-trace     @("--symptom", "4")
Ask s5-log-noise @("--symptom", "5")   # not an incident — a request for a log viewer.
                                       # the test is whether it says so or invents a threshold.

# should decline
Ask x1-csv        @("the CSV export job is failing")
Ask x2-salesforce @("our Salesforce sync stopped last night")
Ask x3-webhooks   @("our webhooks stopped firing")

# is PLATFORM.md doing anything
Ask x3-webhooks-nocontext @("our webhooks stopped firing", "--no-platform-context")

Write-Host ""
Write-Host "=== summary ===" -ForegroundColor Cyan
Select-String -Path examples\*.txt -Pattern "^source:" |
    ForEach-Object { $_.Line } | Group-Object | Select-Object Count, Name

Write-Host "declined:"
Select-String -Path examples\*.txt -Pattern "insufficient evidence" |
    Select-Object -ExpandProperty Filename -Unique | ForEach-Object { "  $_" }

Write-Host "dropped citations:"
$dropped = Select-String -Path examples\*.txt -Pattern "validator dropped"
if ($dropped) { $dropped | ForEach-Object { "  $($_.Filename): $($_.Line.Trim())" } }
else { "  none" }
