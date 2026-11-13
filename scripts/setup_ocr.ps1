param([string]$OcrRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime/ocr-tools'))
$ErrorActionPreference = 'Stop'
$sevenZip = Join-Path $env:ProgramFiles '7-Zip/7z.exe'
if (-not (Test-Path -LiteralPath $sevenZip)) { throw 'Install 7-Zip first; portable extraction does not run the installer.' }
New-Item -ItemType Directory -Path $OcrRoot -Force | Out-Null
$installer = Join-Path $OcrRoot 'tesseract-setup-5.4.0.exe'
$expected = 'C885FFF6998E0608BA4BB8AB51436E1C6775C2BAFC2559A19B423E18678B60C9'
if (-not (Test-Path -LiteralPath $installer)) {
    & curl.exe -L --fail --retry 2 --max-time 180 -o $installer 'https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe'
    if ($LASTEXITCODE -ne 0) { throw 'OCR download failed' }
}
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expected) { throw 'OCR archive checksum mismatch' }
$binaryRoot = Join-Path $OcrRoot 'tesseract'
$binary = Join-Path $binaryRoot 'tesseract.exe'
if (-not (Test-Path -LiteralPath $binary)) {
    & $sevenZip x $installer "-o$binaryRoot" -y -bd
    if ($LASTEXITCODE -gt 1) { throw 'OCR extraction failed' }
}
$tessdata = Join-Path $binaryRoot 'tessdata'
$russian = Join-Path $tessdata 'rus.traineddata'
if (-not (Test-Path -LiteralPath $russian)) {
    & curl.exe -L --fail --retry 2 --max-time 120 -o $russian 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/rus.traineddata'
    if ($LASTEXITCODE -ne 0) { throw 'Russian OCR data download failed' }
}
if ((Get-FileHash -LiteralPath $russian -Algorithm SHA256).Hash -ne 'E16E5E036CCE1D9EC2B00063CF8B54472625B9E14D893A169E2B0DEDEB4DF225') { throw 'Russian OCR data checksum mismatch' }
$version = (& $binary --version 2>&1 | Select-Object -First 1).ToString()
if ($LASTEXITCODE -ne 0) { throw 'OCR executable failed' }
$languages = & $binary --list-langs --tessdata-dir $tessdata
if ($LASTEXITCODE -ne 0 -or 'rus' -notin $languages -or 'eng' -notin $languages -or 'osd' -notin $languages) { throw 'OCR language/orientation data missing' }
@{version=$version; installer_sha256=$expected; russian_sha256=(Get-FileHash -LiteralPath $russian).Hash; binary=$binary; tessdata=$tessdata} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OcrRoot 'receipt.json') -Encoding UTF8
Write-Output "TESSERACT_CMD=$binary"
Write-Output "TESSDATA_PREFIX=$tessdata"
Write-Output 'Portable OCR is ready. Add these values to the local .env. No system PATH or registry was changed.'
