param([string]$OutputDir = (Join-Path $PSScriptRoot 'dist'))

$ErrorActionPreference = 'Stop'
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$buildDir = Join-Path $PSScriptRoot 'build'
$files = @('catalog.json', 'resolve_autocut.py', 'resolve_scene_cuts.py', 'resolve_quality_check.py')
$arguments = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'ResolveScriptsSetup',
    '--distpath', $OutputDir, '--workpath', $buildDir, '--specpath', $buildDir,
    '--hidden-import', 'argparse', '--hidden-import', 'csv',
    '--hidden-import', 'fractions', '--hidden-import', 'xml.etree.ElementTree'
)
foreach ($file in $files) {
    $arguments += @('--add-data', "$(Join-Path $PSScriptRoot $file);.")
}
python -m PyInstaller @arguments (Join-Path $PSScriptRoot 'installer.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
