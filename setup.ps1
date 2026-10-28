# 酷狗一起听 —— 装之前跑这个
#
# 干三件事：
#   1. 找出用哪个 python
#   2. 体检依赖（缺什么、什么版本、怎么补）
#   3. 把 cordis.patch.yml 里的 command / args 改成这台机器的真实路径
#
# 用法（在这个目录里）：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Python "C:\Python311\python.exe"

param(
  [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'tools\kugou_mcp.py'
$patch  = Join-Path $here 'cordis.patch.yml'

function Write-Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

# ── 1. 找 python ─────────────────────────────────────────────
Write-Step '1/3 找 python'

if (-not $Python) {
  foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source; break }
  }
}

if (-not $Python -or -not (Test-Path $Python)) {
  Write-Host '没找到 python。装一个（3.9+），或者手动指定：' -ForegroundColor Yellow
  Write-Host '  .\setup.ps1 -Python "C:\Python311\python.exe"'
  exit 1
}

$ver = & $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
Write-Host ("python: {0}  ({1})" -f $Python, $ver)

# ── 2. 体检依赖 ──────────────────────────────────────────────
Write-Step '2/3 体检依赖'

$out = & $Python $script --deps 2>&1 | Out-String
Write-Host $out

if ($out -match '"ok": false') {
  Write-Host ''
  Write-Host '依赖没齐。把下面这一整行复制到命令行里跑一遍，然后重新跑这个脚本：' -ForegroundColor Yellow
  Write-Host ''
  Write-Host ('  "{0}" -m pip install pillow numpy winocr pyaudiowpatch uiautomation "mcp<2" -i https://pypi.tuna.tsinghua.edu.cn/simple' -f $Python) -ForegroundColor White
  Write-Host ''
  Write-Host '（uiautomation 是点按钮用的，没装也能读歌读词，只是开房/退出会退回截图认字）'
  exit 1
}

# ── 3. 改 patch 里的路径 ─────────────────────────────────────
Write-Step '3/3 写配置路径'

if (-not (Test-Path $patch)) {
  Write-Host "找不到 cordis.patch.yml：$patch" -ForegroundColor Red
  exit 1
}

$lines = Get-Content $patch -Encoding utf8
$new = foreach ($line in $lines) {
  if ($line -match '^\s*command:\s') { '        command: ' + $Python }
  elseif ($line -match '^\s*-\s*.*kugou_mcp\.py\s*$') { '          - ' + $script }
  else { $line }
}
Set-Content -Path $patch -Value $new -Encoding utf8

Write-Host '已写入：'
Select-String -Path $patch -Pattern 'command:|kugou_mcp\.py' | ForEach-Object { '  ' + $_.Line.Trim() }

# 顺手告诉宿主该用哪个 python —— 设置页那些按钮会读这个文件，
# 不然它可能去 PATH 里随便找一个没装 winocr 的 python，一调就 ModuleNotFoundError
$homeDir = Join-Path $env:USERPROFILE '.kugou-listen'
if (-not (Test-Path $homeDir)) { New-Item -ItemType Directory -Path $homeDir -Force | Out-Null }
$pyFile = Join-Path $homeDir 'python.txt'
[System.IO.File]::WriteAllText($pyFile, $Python, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("也记下了该用哪个 python：{0}" -f $pyFile)

Write-Step '下一步'
Write-Host @'
把整个目录装进 DSH：

  用插件管理器的「本地目录安装」指向这个文件夹，
  或者命令行：dsh plugin --profile <你的 profile> add link:<这个目录的绝对路径>

装完**重启一次 DSH**（不是刷新页面）。
重启后设置左侧栏最下面会有「酷狗一起听」。

装不上的时候先跑这两个：

  python tools\kugou_mcp.py --deps        体检依赖
  python tools\kugou_mcp.py --selfcheck   看酷狗读不读得到
'@ -ForegroundColor Gray
