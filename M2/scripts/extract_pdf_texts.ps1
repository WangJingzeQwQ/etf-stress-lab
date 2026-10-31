param(
    [Parameter(Mandatory = $true)]
    [string]$PdfDirectory,

    [Parameter(Mandatory = $true)]
    [string]$TextDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ReportPath,

    [string]$PdfToText = "C:\Program Files\MiKTeX\miktex\bin\x64\pdftotext.exe",

    [ValidateRange(1, 16)]
    [int]$Workers = 4
)

$ErrorActionPreference = "Continue"
$resolvedPdfDirectory = (Resolve-Path -LiteralPath $PdfDirectory).Path
$resolvedTextDirectory = [System.IO.Path]::GetFullPath($TextDirectory)
[System.IO.Directory]::CreateDirectory($resolvedTextDirectory) | Out-Null
$resolvedReportPath = [System.IO.Path]::GetFullPath($ReportPath)
[System.IO.Directory]::CreateDirectory(
    [System.IO.Path]::GetDirectoryName($resolvedReportPath)
) | Out-Null

$files = Get-ChildItem -LiteralPath $resolvedPdfDirectory -Filter "*.pdf" -File
$results = $files | ForEach-Object -Parallel {
    $pdf = $_
    $textPath = Join-Path $using:resolvedTextDirectory ($pdf.BaseName + ".txt")
    $errorText = ""
    try {
        & $using:PdfToText -q -layout -enc UTF-8 $pdf.FullName $textPath 2>$null
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            $errorText = "pdftotext_exit_$exitCode"
        }
        elseif (-not (Test-Path -LiteralPath $textPath)) {
            $errorText = "missing_text_output"
        }
        elseif ((Get-Item -LiteralPath $textPath).Length -eq 0) {
            $errorText = "empty_text_output"
        }
    }
    catch {
        $exitCode = -1
        $errorText = $_.Exception.Message
    }
    [pscustomobject]@{
        document_id = $pdf.BaseName
        pdf_bytes = $pdf.Length
        status = if ($errorText) { "FAILED" } else { "TEXT_EXTRACTED" }
        error = $errorText
    }
} -ThrottleLimit $Workers

$results |
    ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $resolvedReportPath -Encoding utf8

[pscustomobject]@{
    status = if (($results | Where-Object status -eq "FAILED").Count -eq 0) {
        "EXTRACTED"
    } else {
        "PARTIAL"
    }
    pdf_files = $files.Count
    extracted_texts = ($results | Where-Object status -eq "TEXT_EXTRACTED").Count
    failed = ($results | Where-Object status -eq "FAILED").Count
    report = $resolvedReportPath
} | Format-List
