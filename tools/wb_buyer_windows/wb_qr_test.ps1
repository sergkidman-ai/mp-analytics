# поток: ret
# Manual WB buyer login and local screenshot. No uploads, cookie export or scheduled tasks.
param([switch]$LibraryOnly, [switch]$Normal, [ValidateSet('Auto','Chrome','Edge')][string]$Browser = 'Auto')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-LocalCdp {
    param([string]$WebSocketUrl, [string]$Method, [hashtable]$Arguments = @{}, [switch]$CollectLog)
    $uri = [Uri]$WebSocketUrl
    if ($uri.Scheme -ne 'ws' -or -not $uri.IsLoopback) {
        throw 'Разрешено подключение только к браузеру на этом компьютере.'
    }
    $socket = New-Object System.Net.WebSockets.ClientWebSocket
    # Local browser control must not pass through a Windows/corporate HTTP proxy.
    $socket.Options.Proxy = $null
    $cancel = New-Object System.Threading.CancellationTokenSource
    $cancel.CancelAfter(15000)
    $entries = New-Object 'Collections.Generic.List[object]'
    try {
        $null = $socket.ConnectAsync($uri, $cancel.Token).GetAwaiter().GetResult()
        $request = @{id = 1; method = $Method; params = $Arguments} | ConvertTo-Json -Compress -Depth 12
        $bytes = [Text.Encoding]::UTF8.GetBytes($request)
        $segment = [ArraySegment[byte]]::new($bytes)
        $null = $socket.SendAsync($segment, [Net.WebSockets.WebSocketMessageType]::Text, $true, $cancel.Token).GetAwaiter().GetResult()
        $buffer = New-Object byte[] 65536
        while ($true) {
            $message = New-Object IO.MemoryStream
            try {
                do {
                    $received = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $cancel.Token).GetAwaiter().GetResult()
                    if ($received.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) {
                        throw 'Браузер закрыл соединение.'
                    }
                    $message.Write($buffer, 0, $received.Count)
                    if ($message.Length -gt 16777216) { throw 'Слишком большой ответ браузера.' }
                } until ($received.EndOfMessage)
                $reply = [Text.Encoding]::UTF8.GetString($message.ToArray()) | ConvertFrom-Json
                if ($CollectLog -and $reply.PSObject.Properties.Name -contains 'method' -and $reply.method -eq 'Log.entryAdded') {
                    $entry = $reply.params.entry
                    # Keep only error classes/status, never log text, URLs, cookie or headers.
                    $errorCode = [regex]::Match($entry.text, 'net::ERR_[A-Z_]+|TypeError|ReferenceError|SyntaxError|RangeError').Value
                    $status = [regex]::Match($entry.text, '(?:status of |status code[: ]+)([45][0-9]{2})').Groups[1].Value
                    $entries.Add(@{source = $entry.source; level = $entry.level; error_code = $errorCode; http_status = $status})
                }
                if ($reply.PSObject.Properties.Name -contains 'id' -and $reply.id -eq 1) {
                    if ($reply.PSObject.Properties.Name -contains 'error') {
                        throw ('Ошибка браузера: ' + $reply.error.message)
                    }
                    if ($CollectLog) { return @{entries = $entries.ToArray(); buffered_entries_only = $true} }
                    return $reply.result
                }
            } finally { $message.Dispose() }
        }
    } finally {
        $socket.Dispose()
        $cancel.Dispose()
    }
}

function Get-WbPage {
    param([int]$Port)
    # Assign JSON arrays directly: @(...Invoke-RestMethod...) can wrap the whole
    # returned array as one item and accidentally select several browser tabs.
    $pages = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 5
    $wbPages = @($pages | Where-Object {
        $_.type -eq 'page' -and $_.url -match '^https://(www\.)?wildberries\.ru(/|$)'
    })
    if ($wbPages.Count -ne 1) {
        throw 'Нужна одна вкладка Wildberries в тестовом окне. Закройте лишние вкладки или откройте кабинет WB.'
    }
    return $wbPages[0]
}

function Save-WbScreen {
    param([int]$Port, [string]$OutputDirectory)
    $page = Get-WbPage -Port $Port
    $expression = @'
JSON.stringify({
  blocked: /Подозрительная активность|Проверяем браузер/.test(document.body.innerText),
  login: [...document.querySelectorAll('input[type="tel"],input[autocomplete="one-time-code"]')]
    .some(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden'),
  path: location.pathname,
  width: innerWidth,
  height: innerHeight,
  blank: !document.body.innerText.trim() &&
    ![...document.querySelectorAll('img,canvas,svg')].some(e => e.getBoundingClientRect().width >= 60 && e.getBoundingClientRect().height >= 60)
})
'@
    $probe = Invoke-LocalCdp -WebSocketUrl $page.webSocketDebuggerUrl -Method 'Runtime.evaluate' -Arguments @{
        expression = $expression; returnByValue = $true
    }
    if ($probe.PSObject.Properties.Name -contains 'exceptionDetails') { throw 'Не удалось проверить страницу WB.' }
    $state = $probe.result.value | ConvertFrom-Json
    if ($state.blocked) { throw 'WB показывает проверку браузера. Скриншот не сохранён как QR. Передайте текст ошибки.' }
    if ($state.login -or $state.path -match '/security/login') {
        throw 'На странице видна форма входа. Сначала завершите вход в аккаунт Дисквэра.'
    }
    if ($state.blank) { throw 'WB показывает пустую страницу. Сохраните диагностику вместо снимка QR.' }
    $capture = Invoke-LocalCdp -WebSocketUrl $page.webSocketDebuggerUrl -Method 'Page.captureScreenshot' -Arguments @{
        format = 'png'; fromSurface = $true; captureBeyondViewport = $false
    }
    $png = [Convert]::FromBase64String($capture.data)
    $signature = [byte[]](137,80,78,71,13,10,26,10)
    if ($png.Length -lt 8 -or [Convert]::ToBase64String($png[0..7]) -ne [Convert]::ToBase64String($signature)) {
        throw 'Браузер не вернул PNG.'
    }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $imagePath = Join-Path $OutputDirectory 'WB_Diskver_screen.png'
    [IO.File]::WriteAllBytes($imagePath, $png)
    $report = @{
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        account_label = 'Дисквэр (выбран пользователем)'
        account_automatically_verified = $false
        qr_automatically_verified = $false
        screenshot = 'WB_Diskver_screen.png'
        screenshot_sha256 = (Get-FileHash -Path $imagePath -Algorithm SHA256).Hash
        host = ([Uri]$page.url).Host
        page_path = $state.path
        viewport = @{width = $state.width; height = $state.height}
        uploaded = $false
        launcher_version = 2
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'test_report.json') -Encoding UTF8
    return $imagePath
}

function Save-WbDiagnostic {
    param([int]$Port, [string]$OutputDirectory)
    $version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    $pages = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 5
    $tabs = @($pages | Where-Object { $_.type -eq 'page' })
    $summaries = @()
    foreach ($tab in $tabs) {
        $uri = $null
        $summary = @{host = ''; scheme = 'unknown'; is_blank_tab = ($tab.url -eq 'about:blank')}
        if ([Uri]::TryCreate($tab.url, [UriKind]::Absolute, [ref]$uri)) {
            $summary.host = $uri.Host
            $summary.scheme = $uri.Scheme
        }
        if ($tab.url -match '^https://(www\.)?wildberries\.ru(/|$)') {
            try {
                $expression = @'
JSON.stringify({ready_state:document.readyState,
  body_text_length:document.body ? document.body.innerText.length : 0,
  body_children:document.body ? document.body.childElementCount : 0,
  blocked:document.body ? /Подозрительная активность|Проверяем браузер/.test(document.body.innerText) : false,
  resources:performance.getEntriesByType('resource').slice(-80).map(e=>({
    host:(()=>{try{return new URL(e.name).hostname}catch(_){return ''}})(),
    kind:e.initiatorType,status:e.responseStatus || 0,duration_ms:Math.round(e.duration)
  }))})
'@
                $probe = Invoke-LocalCdp -WebSocketUrl $tab.webSocketDebuggerUrl -Method 'Runtime.evaluate' -Arguments @{expression = $expression; returnByValue = $true}
                $summary.page = $probe.result.value | ConvertFrom-Json
                $summary.logs = Invoke-LocalCdp -WebSocketUrl $tab.webSocketDebuggerUrl -Method 'Log.enable' -CollectLog
            } catch { $summary.probe_failed = $true }
        }
        $summaries += $summary
    }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $file = Join-Path $OutputDirectory 'diagnostic.json'
    @{launcher_version = 2; captured_at_utc = [DateTime]::UtcNow.ToString('o');
      browser_version = $version.Browser; tabs = $summaries; uploaded = $false} |
        ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $file -Encoding UTF8
    return $file
}

function Find-TestBrowser {
    param([string]$RequestedBrowser = 'Auto')
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    $locations = @()
    $relativePaths = @()
    if ($RequestedBrowser -in @('Auto','Chrome')) { $relativePaths += 'Google\Chrome\Application\chrome.exe' }
    if ($RequestedBrowser -in @('Auto','Edge')) { $relativePaths += 'Microsoft\Edge\Application\msedge.exe' }
    foreach ($relative in $relativePaths) {
        foreach ($root in $roots) { $locations += Join-Path $root $relative }
    }
    foreach ($location in $locations) {
        if (Test-Path -LiteralPath $location -PathType Leaf) { return $location }
    }
    throw 'Не найден Google Chrome или Microsoft Edge. Сообщите, каким браузером вы пользуетесь.'
}

function Start-WbTest {
    if ($env:OS -ne 'Windows_NT') { throw 'START.cmd рассчитан на Windows.' }
    $browser = Find-TestBrowser -RequestedBrowser $Browser
    $profileName = if ([IO.Path]::GetFileName($browser) -eq 'msedge.exe') { 'EdgeProfile' } else { 'BrowserProfile' }
    $testProfile = Join-Path $env:LOCALAPPDATA ('DiskverWBReturnQR\' + $profileName)
    New-Item -ItemType Directory -Path $testProfile -Force | Out-Null
    $existing = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($testProfile) -and $_.CommandLine.Contains('--user-data-dir')
    })
    if ($existing.Count -gt 0) { throw 'Предыдущее тестовое окно ещё открыто. Закройте его и повторите запуск. Авторизация сохранится.' }
    if ($Normal) {
        Start-Process -FilePath $browser -ArgumentList @(
            ('--user-data-dir="' + $testProfile + '"'), '--no-first-run', '--no-default-browser-check',
            '--new-window', 'https://www.wildberries.ru/') | Out-Null
        Write-Host 'Открыто обычное окно без подключения скрипта. Войдите в Дисквэр через интерфейс главной страницы WB.'
        Write-Host 'Проверьте, открываются ли Заказы и QR. Затем сообщите результат; окно закройте самостоятельно.'
        Write-Host 'Для режима снимка закройте это тестовое окно и запустите CAPTURE.cmd. Вход сохранится.'
        return
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { $port = $listener.LocalEndpoint.Port } finally { $listener.Stop() }
    $browserVersion = $null
    Write-Host 'Откроется ОТДЕЛЬНОЕ окно WB. Войдите в покупательский аккаунт Дисквэра.'
    Write-Host 'Код подтверждения вводите только в окне WB.'
    $launchArguments = @(
        ('--user-data-dir="' + $testProfile + '"'),
        "--remote-debugging-port=$port", '--remote-debugging-address=127.0.0.1',
        '--no-first-run', '--no-default-browser-check', '--new-window', 'https://www.wildberries.ru/'
    )
    Start-Process -FilePath $browser -ArgumentList $launchArguments | Out-Null
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $deadline) {
            try {
                $browserVersion = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 2
                break
            } catch { Start-Sleep -Milliseconds 300 }
        }
        if ($null -eq $browserVersion) {
            throw 'Не удалось подключиться к тестовому браузеру. Закройте предыдущее тестовое окно и повторите запуск.'
        }
        Write-Host ''
        Write-Host '1. Войдите в аккаунт Дисквэра и откройте Заказы.'
        Write-Host '2. Откройте настоящий QR получения крупно на экране.'
        Write-Host '3. Вернитесь в это окно. После сохранения тестовый браузер закроется, профиль останется.'
        $output = Join-Path $PSScriptRoot ('Result\' + [DateTime]::Now.ToString('yyyyMMdd_HHmmss'))
        $answer = Read-Host 'Если QR виден, введите QR и Enter. При белом окне или другой проблеме введите 0 и Enter'
        if ($answer.Trim() -ne 'QR') {
            $diagnostic = Save-WbDiagnostic -Port $port -OutputDirectory $output
            Write-Host ('Диагностика сохранена: ' + $diagnostic)
            Write-Host 'Отправьте diagnostic.json в дропбокс-бот. Снимок страницы и данные входа в него не включены.'
            Start-Process explorer.exe -ArgumentList ('"' + $output + '"') | Out-Null
            return
        }
        try { $image = Save-WbScreen -Port $port -OutputDirectory $output } catch {
            Save-WbDiagnostic -Port $port -OutputDirectory $output | Out-Null
            Write-Host ('Сохранена диагностика: ' + (Join-Path $output 'diagnostic.json'))
            throw
        }
        Write-Host ('Сохранён скриншот: ' + $image)
        Write-Host 'Проверьте изображение и отправьте только PNG и test_report.json в дропбокс-бот для проверки QR.'
        Write-Host 'Папку профиля браузера отправлять не нужно. Автоматическая рассылка не включена.'
        Start-Process explorer.exe -ArgumentList ('"' + $output + '"') | Out-Null
    } finally {
        if ($null -ne $browserVersion) {
            try { Invoke-LocalCdp -WebSocketUrl $browserVersion.webSocketDebuggerUrl -Method 'Browser.close' | Out-Null } catch { }
        }
    }
}

if (-not $LibraryOnly) {
    try { Start-WbTest } catch { Write-Host ('Ошибка: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
