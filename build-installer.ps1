param(
    [string]$OutputDir = (Join-Path $PSScriptRoot 'dist'),
    [Parameter(Mandatory)][string]$FFmpegArchive,
    [Parameter(Mandatory)][string]$RussianModelDirectory,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$tkData = & $Python -c 'import sys, tkinter; print(sys.base_prefix); print(tkinter.TkVersion)'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$baseTcl = Join-Path $tkData[0] 'tcl'
if (-not $env:TCL_LIBRARY -and (Test-Path (Join-Path $baseTcl "tcl$($tkData[1])"))) {
    $env:TCL_LIBRARY = Join-Path $baseTcl "tcl$($tkData[1])"
    $env:TK_LIBRARY = Join-Path $baseTcl "tk$($tkData[1])"
}
& $Python -c 'import tkinter; tkinter.Tcl()'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$FFmpegArchive = (Resolve-Path -LiteralPath $FFmpegArchive).Path
$RussianModelDirectory = (Resolve-Path -LiteralPath $RussianModelDirectory).Path
$expectedHash = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $FFmpegArchive).Hash -ne $expectedHash) {
    throw 'Expected original Gyan FFmpeg 9.0.2 essentials ZIP.'
}
$buildDir = Join-Path $PSScriptRoot 'build'
$runtimeDir = Join-Path $buildDir 'runtime'
$catalogFile = Join-Path $PSScriptRoot 'catalog.json'
$catalog = Get-Content -LiteralPath $catalogFile -Raw -Encoding utf8 | ConvertFrom-Json
$files = @('catalog.json') + @($catalog.scripts.files | Sort-Object -Unique)
$entry = Join-Path $PSScriptRoot 'installer.py'
$arguments = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--workpath', $buildDir, '--specpath', $buildDir,
    '--hidden-import', 'argparse', '--hidden-import', 'csv',
    '--hidden-import', 'fractions', '--hidden-import', 'xml.etree.ElementTree',
    '--hidden-import', 'html', '--hidden-import', 'unicodedata'
)
foreach ($file in $files) {
    $arguments += @('--add-data', "$(Join-Path $PSScriptRoot $file);.")
}
$runtimeArguments = @('--name', 'ResolveScriptsRuntime', '--distpath', $runtimeDir,
    '--hidden-import', 'numpy', '--collect-data', 'pocketsphinx', '--hidden-import', 'pocketsphinx',
    '--copy-metadata', 'numpy', '--copy-metadata', 'pocketsphinx')
& $Python -m PyInstaller @arguments @runtimeArguments $entry
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# The setup carries the archive once. The installed launcher stays small.
$payload = Join-Path $buildDir 'payload'
New-Item -Path $payload -ItemType Directory -Force | Out-Null
$archive = Join-Path $payload 'ffmpeg.zip'
if ($archive -ne $FFmpegArchive) {
    Copy-Item -LiteralPath $FFmpegArchive -Destination $archive -Force
}
$russianModel = Join-Path $payload 'russian-speech.zip'
$binaryLm = Join-Path $RussianModelDirectory 'ru.lm.bin'
if (-not (Test-Path -LiteralPath $binaryLm)) {
    & $Python -c 'import sys; from pocketsphinx import Config, LogMath, NGramModel; NGramModel(Config(lm=None), LogMath(), sys.argv[1]).write(sys.argv[2])' (Join-Path $RussianModelDirectory 'ru.lm') $binaryLm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$modelFiles = @('feat.params', 'feature_transform', 'mdef', 'means', 'mixture_weights',
    'noisedict', 'ru.dic', 'ru.lm.bin', 'transition_matrices', 'variances', 'LICENSE', 'README')
$modelPaths = $modelFiles | ForEach-Object { Join-Path $RussianModelDirectory $_ }
Compress-Archive -LiteralPath $modelPaths -DestinationPath $russianModel -Force
$runtime = Join-Path $runtimeDir 'ResolveScriptsRuntime.exe'
$setupArguments = @('--name', 'ResolveScriptsSetup', '--distpath', $OutputDir,
                    '--add-data', "$archive;.", '--add-data', "$runtime;.",
                    '--add-data', "$russianModel;.")
& $Python -m PyInstaller @arguments @setupArguments $entry
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
