$ErrorActionPreference = 'Stop'
$candidates = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Docker Desktop.lnk'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Docker Desktop.lnk'),
    (Join-Path ([Environment]::GetFolderPath('CommonStartMenu')) 'Programs\Docker Desktop.lnk')
)
$shortcut = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $shortcut) {
    throw 'Existing Docker Desktop shortcut was not found.'
}
$shell = New-Object -ComObject Shell.Application
$handle = 0
$desktop = $shell.Windows().FindWindowSW(0, 0, 8, [ref]$handle, 1)
if ($null -eq $desktop) {
    throw 'Explorer desktop shell is unavailable.'
}
$desktop.Document.Application.ShellExecute($shortcut, '', (Split-Path -Parent $shortcut), 'open', 0)
