$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$code = Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\Code.exe"
if (-not (Test-Path -LiteralPath $code -PathType Leaf)) {
    throw "VS Code bundled Node runtime not found: $code"
}

$previousElectronMode = $env:ELECTRON_RUN_AS_NODE
$tests = @(
    "tools/test_submission_contract.mjs",
    "tools/run_core_tests_node.mjs"
)
try {
    $env:ELECTRON_RUN_AS_NODE = "1"
    foreach ($test in $tests) {
        $stdout = [System.IO.Path]::GetTempFileName()
        $stderr = [System.IO.Path]::GetTempFileName()
        try {
            $process = Start-Process -FilePath $code `
                -ArgumentList $test `
                -WorkingDirectory $repositoryRoot `
                -WindowStyle Hidden `
                -Wait `
                -PassThru `
                -RedirectStandardOutput $stdout `
                -RedirectStandardError $stderr
            Get-Content -LiteralPath $stdout
            Get-Content -LiteralPath $stderr
            if ($process.ExitCode -ne 0) {
                throw "$test failed with exit code $($process.ExitCode)"
            }
        }
        finally {
            Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    $env:ELECTRON_RUN_AS_NODE = $previousElectronMode
}

Write-Output "PASS synchronous frontend/backend Node harnesses"
