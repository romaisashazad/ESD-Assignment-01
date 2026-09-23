# Part E load test: sends a steady stream of QR requests and prints a summary.
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\load-test.ps1 -Minutes 6
param(
    [int]$Minutes = 6,
    [double]$IntervalSeconds = 0.5,
    [string]$Url = "http://localhost:5000/generate"
)

$start = Get-Date
$end = $start.AddMinutes($Minutes)
$i = 0
$failures = 0
$durations = New-Object System.Collections.Generic.List[double]

Write-Host ("Load test started {0}, running {1} min, one request every {2}s" -f $start.ToString("HH:mm:ss"), $Minutes, $IntervalSeconds)

while ((Get-Date) -lt $end) {
    $i++
    # Alternate between URL and plain-text payloads (exercises both "kind" values).
    $text = if ($i % 2 -eq 0) { "https://example.com/item/$i" } else { "hello $i" }
    $body = @{ text = $text } | ConvertTo-Json

    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri $Url -Method POST -ContentType "application/json" -Body $body -UseBasicParsing
        $status = $r.StatusCode
        $rid = $r.Headers["X-Request-ID"]
    } catch {
        $status = "ERR"
        $rid = "-"
        $failures++
    }
    $sw.Stop()
    $ms = $sw.Elapsed.TotalMilliseconds
    $durations.Add($ms)

    "{0}  #{1,-4} status={2}  {3,6:N0} ms  id={4}" -f (Get-Date -Format "HH:mm:ss"), $i, $status, $ms, $rid
    Start-Sleep -Milliseconds ([int]($IntervalSeconds * 1000))
}

$sorted = $durations | Sort-Object
$p95 = $sorted[[math]::Ceiling(0.95 * $sorted.Count) - 1]
$avg = ($durations | Measure-Object -Average).Average
$slow = ($durations | Where-Object { $_ -ge 400 }).Count

Write-Host ""
Write-Host "===== Summary ====="
Write-Host ("Window:        {0} -> {1}" -f $start.ToString("HH:mm:ss"), (Get-Date).ToString("HH:mm:ss"))
Write-Host ("Requests:      {0}  (failures: {1})" -f $i, $failures)
Write-Host ("Average:       {0:N0} ms (client-side, includes network)" -f $avg)
Write-Host ("p95:           {0:N0} ms" -f $p95)
Write-Host ("Slow (>=400ms): {0}  ({1:P0})" -f $slow, ($slow / [math]::Max($i, 1)))
