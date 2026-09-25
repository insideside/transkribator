# Удаление ярлыков Транскрибатора. Саму папку приложения (с моделями и историей) удалите вручную.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = Join-Path $dir "Транскрибатор.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk }
}
Write-Host "Ярлыки удалены."
Write-Host "Чтобы удалить приложение полностью (модели ~6 ГБ, история и загруженные записи), удалите папку:"
Write-Host "  $Root"
