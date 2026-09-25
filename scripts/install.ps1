# ─────────────────────────────────────────────────────────────
#  Транскрибатор — установка на Windows 10/11 (64-бит)
#  Запускается из install.bat. Параметры:
#    -AllModels      скачать обе модели (точную и быструю)
#    -Model <имя>    large-v3 | large-v3-turbo
#    -NoShortcut     не создавать ярлыки
#    -Cpu            не ставить библиотеки CUDA, даже если есть NVIDIA
#    -Ci             для автотестов: маленькая модель, без ярлыков и вопросов
# ─────────────────────────────────────────────────────────────
param(
    [switch]$AllModels,
    [string]$Model = "",
    [switch]$NoShortcut,
    [switch]$Cpu,
    [switch]$Ci
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ""
    Write-Host "Ошибка: $text" -ForegroundColor Red
    exit 1
}

Step "Транскрибатор — установка"
Write-Host "Папка приложения: $Root"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Нужна 64-битная Windows 10 или 11." }
Write-Host "Нужно ~6 ГБ свободного места и интернет только на время установки."

# ── 1. uv ──
Step "1/4  Менеджер окружения uv"
$uvDirs = @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")
foreach ($d in $uvDirs) { if (Test-Path $d) { $env:Path = "$d;$env:Path" } }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Устанавливаю uv (https://docs.astral.sh/uv/)..."
    try {
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    } catch { Fail "Не удалось установить uv. Проверьте интернет." }
    foreach ($d in $uvDirs) { if (Test-Path $d) { $env:Path = "$d;$env:Path" } }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "uv установлен, но не найден. Перезапустите install.bat." }
}
Write-Host ("uv: " + (uv --version))

# ── 2. Python и зависимости ──
Step "2/4  Python и библиотеки"
$syncArgs = @("sync", "--frozen", "--no-dev")
$hasNvidia = $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$useCuda = $hasNvidia -and -not $Cpu -and -not $Ci
$settings = Join-Path $Root "settings.env"
if ($useCuda) {
    Write-Host "Найдена видеокарта NVIDIA — ставлю библиотеки CUDA (≈1,3 ГБ) для быстрого распознавания"
    $syncArgs += @("--extra", "cuda")
    if (Test-Path $settings) { Remove-Item $settings }
} else {
    Write-Host "Распознавание будет идти на процессоре (видеокарта NVIDIA не найдена или выбран -Cpu)"
    # без библиотек CUDA видеокарту не использовать
    [System.IO.File]::WriteAllText($settings, "TRANSKRIBATOR_DEVICE=cpu`n")
}
& uv @syncArgs
if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить зависимости." }
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Fail "Не найден $Py" }

# ── 3. Модели ──
Step "3/4  Модели (распознавание и разделение по голосам)"
$modelArgs = @("-m", "app.models")
if ($Ci) { $modelArgs += @("--whisper", "tiny") }
elseif ($AllModels) { $modelArgs += "--all" }
elseif ($Model) { $modelArgs += @("--whisper", $Model) }
& $Py @modelArgs
if ($LASTEXITCODE -ne 0) { Fail "Не удалось скачать модели. Запустите установку ещё раз — скачанное сохранится." }

Step "Проверка"
$testArgs = @("-m", "app.selftest")
if ($Ci) { $testArgs += @("--model", "tiny") }
& $Py @testArgs
if ($LASTEXITCODE -ne 0 -and $useCuda) {
    Write-Host "С видеокартой не заработало — переключаю распознавание на процессор и проверяю снова" -ForegroundColor Yellow
    [System.IO.File]::WriteAllText($settings, "TRANSKRIBATOR_DEVICE=cpu`n")
    $env:TRANSKRIBATOR_DEVICE = "cpu"
    & $Py @testArgs
}
if ($LASTEXITCODE -ne 0) { Fail "Самопроверка не прошла — см. сообщения выше и data\logs\app.log" }

# ── 4. Ярлыки ──
Step "4/4  Ярлыки"
if (-not $NoShortcut -and -not $Ci) {
    $shell = New-Object -ComObject WScript.Shell
    $targets = @(
        [Environment]::GetFolderPath("Desktop"),
        [Environment]::GetFolderPath("Programs")
    )
    foreach ($dir in $targets) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "Транскрибатор.lnk"))
        $lnk.TargetPath = Join-Path $Root "start.bat"
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = (Join-Path $Root "assets\icon.ico") + ",0"
        $lnk.Description = "Локальная расшифровка аудио"
        $lnk.WindowStyle = 7   # окно сервера свёрнуто
        $lnk.Save()
    }
    Write-Host "Ярлыки: на рабочем столе и в меню «Пуск»"
}

Step "Готово!"
Write-Host "Запуск: ярлык «Транскрибатор» на рабочем столе (или start.bat в папке приложения)."
Write-Host "Откроется в браузере: http://127.0.0.1:8770"
Write-Host "Чтобы остановить — закройте окно «Transkribator» на панели задач."
