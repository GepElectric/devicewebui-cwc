[CmdletBinding()]
param(
  [int]$Port = 8766
)

Add-Type -AssemblyName System.Web
[System.Net.ServicePointManager]::SecurityProtocol = `
  [System.Net.SecurityProtocolType]::Tls -bor `
  [System.Net.SecurityProtocolType]::Tls11 -bor `
  [System.Net.SecurityProtocolType]::Tls12

$script:LastTargetByClient = [System.Collections.Concurrent.ConcurrentDictionary[string, string]]::new()
$script:ProxyPayloadCache = [System.Collections.Concurrent.ConcurrentDictionary[string, object]]::new()
$script:CookieJarDirectory = Join-Path $env:TEMP "DeviceWebUIProxyCookies"
if (-not (Test-Path -LiteralPath $script:CookieJarDirectory)) {
  New-Item -ItemType Directory -Path $script:CookieJarDirectory | Out-Null
}

function Set-CorsHeaders {
  param($Response)
  $Response.Headers["Access-Control-Allow-Origin"] = "*"
  $Response.Headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
  $Response.Headers["Access-Control-Allow-Headers"] = "Content-Type"
}

function Normalize-WindowsPath {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return ""
  }

  $normalized = $Value.Trim().Replace("/", "\")
  while ($normalized.EndsWith("\")) {
    $normalized = $normalized.Substring(0, $normalized.Length - 1)
  }
  return $normalized
}

function Resolve-SafePath {
  param(
    [string]$Root,
    [string]$RelativePath
  )

  $normalizedRoot = Normalize-WindowsPath $Root
  if ([string]::IsNullOrWhiteSpace($normalizedRoot)) {
    throw "Root folder is empty."
  }

  if (-not (Test-Path -LiteralPath $normalizedRoot -PathType Container)) {
    throw "Root folder does not exist."
  }

  $rootItem = Get-Item -LiteralPath $normalizedRoot
  $rootFullPath = $rootItem.FullName
  $combinedPath = if ([string]::IsNullOrWhiteSpace($RelativePath)) {
    $rootFullPath
  } else {
    [System.IO.Path]::GetFullPath((Join-Path $rootFullPath $RelativePath))
  }

  $rootWithSlash = if ($rootFullPath.EndsWith("\")) { $rootFullPath } else { $rootFullPath + "\" }
  if ($combinedPath -ne $rootFullPath -and -not $combinedPath.StartsWith($rootWithSlash, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Requested path is outside the root folder."
  }

  return $combinedPath
}

function Get-RelativePath {
  param(
    [string]$Root,
    [string]$FullPath
  )

  $rootPath = Normalize-WindowsPath $Root
  $fullPath = Normalize-WindowsPath $FullPath
  if (-not $rootPath) {
    return ""
  }

  $rootWithSlash = if ($rootPath.EndsWith("\")) { $rootPath } else { $rootPath + "\" }
  if ($fullPath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    return ""
  }
  if ($fullPath.StartsWith($rootWithSlash, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $fullPath.Substring($rootWithSlash.Length)
  }
  return ""
}

function Write-JsonResponse {
  param(
    $Response,
    [int]$StatusCode,
    $Payload
  )

  $Response.StatusCode = $StatusCode
  $Response.ContentType = "application/json; charset=utf-8"
  Set-CorsHeaders $Response
  $json = $Payload | ConvertTo-Json -Depth 8
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  $Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Write-TextResponse {
  param(
    $Response,
    [int]$StatusCode,
    [string]$Text
  )

  $Response.StatusCode = $StatusCode
  $Response.ContentType = "text/plain; charset=utf-8"
  Set-CorsHeaders $Response
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
  $Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Get-TargetUri {
  param(
    [string]$Value,
    [System.Uri]$Referrer,
    [string]$FallbackTarget
  )

  $trimmed = [string]$Value
  if ([string]::IsNullOrWhiteSpace($trimmed) -and $Referrer) {
    $refQuery = [System.Web.HttpUtility]::ParseQueryString($Referrer.Query)
    $trimmed = [string]$refQuery["target"]
  }
  if ([string]::IsNullOrWhiteSpace($trimmed) -and -not [string]::IsNullOrWhiteSpace($FallbackTarget)) {
    $trimmed = [string]$FallbackTarget
  }

  if ([string]::IsNullOrWhiteSpace($trimmed)) {
    throw "Target URL is empty."
  }

  $trimmed = $trimmed.Trim()
  if (-not ($trimmed.StartsWith("http://", [System.StringComparison]::OrdinalIgnoreCase) -or $trimmed.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase))) {
    $trimmed = "https://$trimmed"
  }

  $uri = $null
  if (-not [System.Uri]::TryCreate($trimmed, [System.UriKind]::Absolute, [ref]$uri)) {
    throw "Target URL is invalid."
  }

  return $uri
}

function Get-ProxyUrl {
  param(
    [int]$PortNumber,
    [string]$TargetUrl
  )

  return "http://127.0.0.1:$PortNumber/proxy?target=$([System.Uri]::EscapeDataString($TargetUrl))"
}

function Get-CacheLifetimeSeconds {
  param(
    [string]$MediaType,
    [System.Uri]$FinalUri
  )

  $path = if ($FinalUri) { $FinalUri.AbsolutePath } else { "" }
  if ($MediaType -and $MediaType.StartsWith("text/html", [System.StringComparison]::OrdinalIgnoreCase)) {
    return 10
  }
  if ($MediaType -and (
      $MediaType.StartsWith("text/css", [System.StringComparison]::OrdinalIgnoreCase) -or
      $MediaType.StartsWith("application/javascript", [System.StringComparison]::OrdinalIgnoreCase) -or
      $MediaType.StartsWith("text/javascript", [System.StringComparison]::OrdinalIgnoreCase) -or
      $MediaType.StartsWith("application/x-javascript", [System.StringComparison]::OrdinalIgnoreCase) -or
      $MediaType.StartsWith("image/", [System.StringComparison]::OrdinalIgnoreCase) -or
      $MediaType.StartsWith("font/", [System.StringComparison]::OrdinalIgnoreCase)
    )) {
    return 3600
  }
  if ($path -match '\.(css|js|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|eot)$') {
    return 3600
  }
  return 0
}

function Try-GetCachedProxyPayload {
  param([string]$CacheKey)

  if ([string]::IsNullOrWhiteSpace($CacheKey)) {
    return $null
  }

  $entry = $null
  if (-not $script:ProxyPayloadCache.TryGetValue($CacheKey, [ref]$entry)) {
    return $null
  }

  if (-not $entry) {
    return $null
  }

  if ($entry.ExpiresAtUtc -le [DateTime]::UtcNow) {
    $removed = $null
    [void]$script:ProxyPayloadCache.TryRemove($CacheKey, [ref]$removed)
    return $null
  }

  return $entry.Payload
}

function Set-CachedProxyPayload {
  param(
    [string]$CacheKey,
    $Payload,
    [int]$LifetimeSeconds
  )

  if ([string]::IsNullOrWhiteSpace($CacheKey) -or -not $Payload -or $LifetimeSeconds -le 0) {
    return
  }

  $entry = [pscustomobject]@{
    ExpiresAtUtc = [DateTime]::UtcNow.AddSeconds($LifetimeSeconds)
    Payload = $Payload
  }

  $script:ProxyPayloadCache[$CacheKey] = $entry
}

function Get-ClientKey {
  param($Request)

  if ($Request.RemoteEndPoint) {
    return ($Request.RemoteEndPoint.Address.ToString() -replace '[^A-Za-z0-9\-_\.]', '_')
  }
  return "unknown"
}

function Get-CookieJarPath {
  param([string]$ClientKey)

  return (Join-Path $script:CookieJarDirectory ($ClientKey + ".txt"))
}

function Get-UpstreamReferrer {
  param([System.Uri]$Referrer)

  if (-not $Referrer) {
    return ""
  }

  $refQuery = [System.Web.HttpUtility]::ParseQueryString($Referrer.Query)
  return [string]$refQuery["target"]
}

function Rewrite-JavaScriptContent {
  param([string]$Script)

  $rewritten = $Script
  $replacements = @(
    @{ Pattern = 'window\.top\.document'; Replacement = 'document' },
    @{ Pattern = 'top\.window\.document'; Replacement = 'document' },
    @{ Pattern = 'top\.document'; Replacement = 'document' },
    @{ Pattern = 'window\.top\.location'; Replacement = 'window.location' },
    @{ Pattern = 'top\.window\.location'; Replacement = 'window.location' },
    @{ Pattern = 'top\.location'; Replacement = 'window.location' },
    @{ Pattern = 'top\.server_frame'; Replacement = 'window.server_frame' },
    @{ Pattern = 'parent\.document'; Replacement = 'document' },
    @{ Pattern = 'parent\.frames'; Replacement = 'window.frames' },
    @{ Pattern = 'parent\.\$'; Replacement = 'window.$' },
    @{ Pattern = 'parent\.leds'; Replacement = 'window.leds' },
    @{ Pattern = 'parent\.update_interval'; Replacement = 'window.update_interval' },
    @{ Pattern = 'parent\.update_phrase_on'; Replacement = 'window.update_phrase_on' },
    @{ Pattern = 'parent\.update_phrase_off'; Replacement = 'window.update_phrase_off' },
    @{ Pattern = 'parent\.start_update'; Replacement = 'window.start_update' },
    @{ Pattern = 'parent\.UpdatePageDataManager'; Replacement = 'window.UpdatePageDataManager' },
    @{ Pattern = 'parent\.sortables_init_fb_update'; Replacement = 'window.sortables_init_fb_update' },
    @{ Pattern = 'frames\.server_frame'; Replacement = 'window.frames.server_frame' },
    @{ Pattern = 'top\.doUpdate'; Replacement = 'window.doUpdate' }
  )

  foreach ($item in $replacements) {
    $rewritten = [System.Text.RegularExpressions.Regex]::Replace(
      $rewritten,
      $item.Pattern,
      $item.Replacement
    )
  }

  return $rewritten
}

function Rewrite-HtmlContent {
  param(
    [string]$Html,
    [System.Uri]$FinalUri,
    [int]$PortNumber
  )

  $baseHref = $FinalUri.GetLeftPart([System.UriPartial]::Path)
  $proxyBase = Get-ProxyUrl -PortNumber $PortNumber -TargetUrl $baseHref
  $escapedBaseHref = [System.Web.HttpUtility]::HtmlAttributeEncode($baseHref)
  $escapedProxyBase = [System.Web.HttpUtility]::JavaScriptStringEncode($proxyBase)

  $withoutMetaCsp = [System.Text.RegularExpressions.Regex]::Replace(
    $Html,
    '<meta[^>]+http-equiv\s*=\s*["'']Content-Security-Policy["''][^>]*>',
    '',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
  )

  if ($withoutMetaCsp -match '(?i)<head[^>]*>') {
    $withoutMetaCsp = [System.Text.RegularExpressions.Regex]::Replace(
      $withoutMetaCsp,
      '(?i)<head([^>]*)>',
      "<head`$1><base href=""$escapedBaseHref"">",
      1
    )
  }

  $scriptBlocks = [System.Collections.Generic.List[string]]::new()
  $withoutMetaCsp = [System.Text.RegularExpressions.Regex]::Replace(
    $withoutMetaCsp,
    '(?is)<script\b([^>]*)>(.*?)</script>',
    {
      param($match)
      $attributes = $match.Groups[1].Value
      $body = $match.Groups[2].Value
      if ($attributes -match '(?i)\bsrc\s*=') {
        return $match.Value
      }
      $rewrittenBody = Rewrite-JavaScriptContent -Script $body
      $placeholder = "__DEVICEWEBUI_SCRIPT_BLOCK_{0}__" -f $scriptBlocks.Count
      $scriptBlocks.Add("<script$attributes>$rewrittenBody</script>")
      return $placeholder
    }
  )

  $rewritten = [System.Text.RegularExpressions.Regex]::Replace(
    $withoutMetaCsp,
    '(?i)\b(href|src|action)=("([^"]*)"|''([^'']*)'')',
    {
      param($match)
      $attribute = $match.Groups[1].Value
      $quoteWrapped = $match.Groups[2].Value
      $rawValue = if ($match.Groups[3].Success) { $match.Groups[3].Value } else { $match.Groups[4].Value }

      if ([string]::IsNullOrWhiteSpace($rawValue)) { return $match.Value }
      if ($rawValue.StartsWith("#")) { return $match.Value }
      if ($rawValue.StartsWith("javascript:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }
      if ($rawValue.StartsWith("data:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }
      if ($rawValue.StartsWith("mailto:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }
      if ($rawValue.StartsWith("tel:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }

      try {
        $absoluteUri = [System.Uri]::new($FinalUri, $rawValue)
        $proxyUrl = Get-ProxyUrl -PortNumber $PortNumber -TargetUrl $absoluteUri.AbsoluteUri
        return "$attribute=""$([System.Web.HttpUtility]::HtmlAttributeEncode($proxyUrl))"""
      } catch {
        return $match.Value
      }
    }
  )

  $rewritten = [System.Text.RegularExpressions.Regex]::Replace(
    $rewritten,
    '(?i)<meta([^>]+http-equiv\s*=\s*["'']refresh["''][^>]+content\s*=\s*["''])([^"''>]+)(["''][^>]*)>',
    {
      param($match)
      $prefix = $match.Groups[1].Value
      $contentValue = $match.Groups[2].Value
      $suffix = $match.Groups[3].Value
      if ($contentValue -match '^\s*(\d+)\s*;\s*url\s*=\s*(.+)\s*$') {
        $delay = $Matches[1]
        $targetPart = $Matches[2].Trim().Trim('"').Trim("'")
        try {
          $absoluteUri = [System.Uri]::new($FinalUri, $targetPart)
          $proxyUrl = Get-ProxyUrl -PortNumber $PortNumber -TargetUrl $absoluteUri.AbsoluteUri
          return "<meta$prefix$delay; URL=$([System.Web.HttpUtility]::HtmlAttributeEncode($proxyUrl))$suffix>"
        } catch {
          return $match.Value
        }
      }
      return $match.Value
    }
  )

  for ($i = 0; $i -lt $scriptBlocks.Count; $i++) {
    $placeholder = "__DEVICEWEBUI_SCRIPT_BLOCK_{0}__" -f $i
    $rewritten = $rewritten.Replace($placeholder, $scriptBlocks[$i])
  }

  $proxyScript = @"
<script>
(function () {
  var proxyRoot = 'http://127.0.0.1:$PortNumber/proxy?target=';
  var proxyPrefixPattern = /^http:\/\/127\.0\.0\.1:$PortNumber\/proxy\?target=/i;
  var currentBase = '$([System.Web.HttpUtility]::JavaScriptStringEncode($baseHref))';
  function toAbsolute(input) {
    try { return new URL(input, currentBase).toString(); } catch (_error) { return input; }
  }
  function proxify(input) {
    if (typeof input !== 'string') { return input; }
    if (!input) { return input; }
    if (proxyPrefixPattern.test(input)) { return input; }
    var absolute = toAbsolute(input);
    if (!absolute) { return input; }
    if (proxyPrefixPattern.test(absolute)) { return absolute; }
    if (/^(mailto:|tel:|javascript:|data:|#)/i.test(absolute)) { return input; }
    return proxyRoot + encodeURIComponent(absolute);
  }
  document.addEventListener('click', function (event) {
    var anchor = event.target.closest('a[href]');
    if (!anchor) { return; }
    var href = anchor.getAttribute('href');
    if (!href || href[0] === '#') { return; }
    event.preventDefault();
    window.location.href = proxify(href);
  }, true);
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form) { return; }
    var rawAction = form.getAttribute('action') || form.action;
    if (!rawAction) { return; }
    form.action = proxify(rawAction);
  }, true);
  var originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function (input, init) {
      if (typeof input === 'string') {
        input = proxify(input);
      } else if (input && input.url) {
        input = proxify(input.url);
      }
      return originalFetch.call(this, input, init);
    };
  }
  if (window.XMLHttpRequest) {
    var originalOpen = window.XMLHttpRequest.prototype.open;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      if (typeof url === 'string') {
        arguments[1] = proxify(url);
      }
      return originalOpen.apply(this, arguments);
    };
  }
  if (window.WebSocket) {
    var OriginalWebSocket = window.WebSocket;
    window.WebSocket = function (url, protocols) {
      var absolute = toAbsolute(url);
      return protocols ? new OriginalWebSocket(absolute, protocols) : new OriginalWebSocket(absolute);
    };
    window.WebSocket.prototype = OriginalWebSocket.prototype;
  }
  var originalOpenWindow = window.open;
  if (originalOpenWindow) {
    window.open = function (url, target, features) {
      if (typeof url === 'string') {
        url = proxify(url);
      }
      if (url) {
        window.location.href = url;
      }
      return null;
    };
  }
})();
</script>
"@

  if ($rewritten -match '(?i)</body>') {
    return [System.Text.RegularExpressions.Regex]::Replace($rewritten, '(?i)</body>', "$proxyScript</body>", 1)
  }

  return $rewritten + $proxyScript
}

function Rewrite-CssContent {
  param(
    [string]$Css,
    [System.Uri]$FinalUri,
    [int]$PortNumber
  )

  return [System.Text.RegularExpressions.Regex]::Replace(
    $Css,
    '(?i)url\(\s*([''"]?)([^)''"]+)\1\s*\)',
    {
      param($match)
      $rawValue = $match.Groups[2].Value.Trim()
      if ([string]::IsNullOrWhiteSpace($rawValue)) { return $match.Value }
      if ($rawValue.StartsWith("data:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }
      if ($rawValue.StartsWith("javascript:", [System.StringComparison]::OrdinalIgnoreCase)) { return $match.Value }
      if ($rawValue.StartsWith("#")) { return $match.Value }

      try {
        $absoluteUri = [System.Uri]::new($FinalUri, $rawValue)
        $proxyUrl = Get-ProxyUrl -PortNumber $PortNumber -TargetUrl $absoluteUri.AbsoluteUri
        return "url(""$proxyUrl"")"
      } catch {
        return $match.Value
      }
    }
  )
}

function Build-ProxiedPayload {
  param(
    [hashtable]$UpstreamResponse,
    [System.Uri]$FinalUri,
    [int]$PortNumber
  )

  $contentType = if ($UpstreamResponse.ContentType) { $UpstreamResponse.ContentType } else { "application/octet-stream" }
  $mediaType = if ($UpstreamResponse.MediaType) { $UpstreamResponse.MediaType } else { "" }
  $statusCode = [int]$UpstreamResponse.StatusCode

  $headers = @{}

  foreach ($header in $UpstreamResponse.Headers.GetEnumerator()) {
    foreach ($value in @($header.Value)) {
      switch -Regex ($header.Key) {
        '^(X-Frame-Options|Content-Security-Policy|Content-Security-Policy-Report-Only|Frame-Options|Transfer-Encoding|Content-Length)$' { continue }
        '^Location$' {
          $redirectUri = [System.Uri]::new($FinalUri, $value)
          $headers["Location"] = Get-ProxyUrl -PortNumber $PortNumber -TargetUrl $redirectUri.AbsoluteUri
          continue
        }
        default {
          $headers[$header.Key] = $value
        }
      }
    }
  }

  $payloadBytes = $null

  if ($mediaType -and $mediaType.StartsWith("text/html", [System.StringComparison]::OrdinalIgnoreCase)) {
    $html = [System.Text.Encoding]::UTF8.GetString($UpstreamResponse.BodyBytes)
    $rewrittenHtml = Rewrite-HtmlContent -Html $html -FinalUri $FinalUri -PortNumber $PortNumber
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($rewrittenHtml)
  } elseif ($mediaType -and $mediaType.StartsWith("text/css", [System.StringComparison]::OrdinalIgnoreCase)) {
    $css = [System.Text.Encoding]::UTF8.GetString($UpstreamResponse.BodyBytes)
    $rewrittenCss = Rewrite-CssContent -Css $css -FinalUri $FinalUri -PortNumber $PortNumber
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($rewrittenCss)
  } elseif ($mediaType -and (
      $mediaType.StartsWith("application/javascript", [System.StringComparison]::OrdinalIgnoreCase) -or
      $mediaType.StartsWith("text/javascript", [System.StringComparison]::OrdinalIgnoreCase) -or
      $mediaType.StartsWith("application/x-javascript", [System.StringComparison]::OrdinalIgnoreCase)
    )) {
    $scriptText = [System.Text.Encoding]::UTF8.GetString($UpstreamResponse.BodyBytes)
    $rewrittenScript = Rewrite-JavaScriptContent -Script $scriptText
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($rewrittenScript)
  } else {
    $payloadBytes = $UpstreamResponse.BodyBytes
  }

  return @{
    StatusCode = $statusCode
    ContentType = $contentType
    Headers = $headers
    BodyBytes = $payloadBytes
    MediaType = $mediaType
  }
}

function Write-ProxiedPayload {
  param(
    $Response,
    [hashtable]$Payload
  )

  $Response.StatusCode = [int]$Payload.StatusCode
  $Response.ContentType = $Payload.ContentType
  Set-CorsHeaders $Response
  $Response.Headers.Remove("X-Frame-Options")
  $Response.Headers.Remove("Content-Security-Policy")
  $Response.Headers.Remove("Content-Security-Policy-Report-Only")
  $Response.Headers.Remove("Frame-Options")

  foreach ($header in $Payload.Headers.GetEnumerator()) {
    $Response.Headers[$header.Key] = $header.Value
  }

  if (($Payload.CacheSeconds | ForEach-Object { [int]$_ }) -gt 0) {
    $Response.Headers["Cache-Control"] = "public, max-age=$($Payload.CacheSeconds)"
  } else {
    $Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
  }

  $bytes = $Payload.BodyBytes
  $Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Invoke-CurlProxyRequest {
  param(
    [System.Uri]$TargetUri,
    $Request,
    [string]$CookieJarPath,
    [string]$UpstreamReferrer
  )

  $headersFile = [System.IO.Path]::GetTempFileName()
  $bodyFile = [System.IO.Path]::GetTempFileName()
  $effectiveUrlFile = [System.IO.Path]::GetTempFileName()

  try {
    $curlArgs = @(
      '--insecure',
      '--location',
      '--silent',
      '--show-error',
      '--dump-header', $headersFile,
      '--output', $bodyFile,
      '--write-out', '%{url_effective}',
      '--cookie', $CookieJarPath,
      '--cookie-jar', $CookieJarPath,
      '--url', $TargetUri.AbsoluteUri
    )

    if ($Request.UserAgent) {
      $curlArgs += @('--user-agent', $Request.UserAgent)
    } else {
      $curlArgs += @('--user-agent', 'DeviceWebUIProxy/1.0')
    }

    if ($Request.Headers['Accept']) {
      $curlArgs += @('--header', ('Accept: ' + $Request.Headers['Accept']))
    }
    if ($Request.Headers['Accept-Language']) {
      $curlArgs += @('--header', ('Accept-Language: ' + $Request.Headers['Accept-Language']))
    }
    if ($UpstreamReferrer) {
      $curlArgs += @('--referer', $UpstreamReferrer)
    }

    if ($Request.HttpMethod -and $Request.HttpMethod -ne 'GET' -and $Request.HttpMethod -ne 'HEAD') {
      $curlArgs += @('-X', $Request.HttpMethod)
      if ($Request.ContentType) {
        $curlArgs += @('--header', ('Content-Type: ' + $Request.ContentType))
      }
      $memory = New-Object System.IO.MemoryStream
      $Request.InputStream.CopyTo($memory)
      $bodyBytes = $memory.ToArray()
      if ($bodyBytes.Length -gt 0) {
        $postFile = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllBytes($postFile, $bodyBytes)
        $curlArgs += @('--data-binary', ('@' + $postFile))
      }
    }

    & curl.exe @curlArgs | Out-File -FilePath $effectiveUrlFile -Encoding ascii
    if ($LASTEXITCODE -ne 0) {
      throw "curl failed with exit code $LASTEXITCODE."
    }

    $headerText = [System.IO.File]::ReadAllText($headersFile)
    $headerBlocks = $headerText -split "(\r?\n){2,}"
    $lastHeaderBlock = ($headerBlocks | Where-Object { $_ -match '^HTTP/' } | Select-Object -Last 1)
    if (-not $lastHeaderBlock) {
      throw "curl did not return HTTP headers."
    }

    $headerLines = $lastHeaderBlock -split "\r?\n" | Where-Object { $_ -and $_.Trim() }
    $statusLine = $headerLines[0]
    if ($statusLine -notmatch '^HTTP/\S+\s+(\d{3})') {
      throw "Unable to parse upstream status code."
    }

    $headers = @{}
    foreach ($line in $headerLines[1..($headerLines.Count - 1)]) {
      $separatorIndex = $line.IndexOf(":")
      if ($separatorIndex -lt 1) { continue }
      $name = $line.Substring(0, $separatorIndex).Trim()
      $value = $line.Substring($separatorIndex + 1).Trim()
      if ($headers.ContainsKey($name)) {
        $existing = @($headers[$name])
        $headers[$name] = $existing + $value
      } else {
        $headers[$name] = @($value)
      }
    }

    $effectiveUrl = [System.IO.File]::ReadAllText($effectiveUrlFile).Trim()
    $bodyBytes = [System.IO.File]::ReadAllBytes($bodyFile)
    $contentType = if ($headers.ContainsKey("content-type")) { $headers["content-type"][-1] } elseif ($headers.ContainsKey("Content-Type")) { $headers["Content-Type"][-1] } else { "application/octet-stream" }
    $mediaType = ($contentType -split ';')[0].Trim()

    return @{
      StatusCode = [int]$Matches[1]
      Headers = $headers
      ContentType = $contentType
      MediaType = $mediaType
      EffectiveUri = [System.Uri]$effectiveUrl
      BodyBytes = $bodyBytes
    }
  } finally {
    if ($postFile) {
      Remove-Item $postFile -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $headersFile, $bodyFile, $effectiveUrlFile -Force -ErrorAction SilentlyContinue
  }
}

function Get-AutoForwardTarget {
  param(
    [System.Uri]$FinalUri,
    [hashtable]$UpstreamResponse
  )

  if (-not $FinalUri -or -not $UpstreamResponse) {
    return $null
  }

  if (-not ($UpstreamResponse.MediaType -and $UpstreamResponse.MediaType.StartsWith("text/html", [System.StringComparison]::OrdinalIgnoreCase))) {
    return $null
  }

  $absolutePath = $FinalUri.AbsolutePath
  if (-not $absolutePath.EndsWith("/Portal/Intro.mwsl", [System.StringComparison]::OrdinalIgnoreCase)) {
    return $null
  }

  $html = [System.Text.Encoding]::UTF8.GetString($UpstreamResponse.BodyBytes)
  if ($html -notmatch '(?i)class="enterformclass"' -or $html -notmatch '(?i)Portal\.mwsl') {
    return $null
  }

  return [System.Uri]::new($FinalUri, "../Portal/Portal.mwsl?PriNav=Start&coming_from_intro=true")
}

function Process-RequestContext {
  param($Context)

  $request = $Context.Request
  $response = $Context.Response

  try {
      if ($request.HttpMethod -eq "OPTIONS") {
        Set-CorsHeaders $response
        $response.StatusCode = 204
        $response.Close()
        continue
      }

      $query = [System.Web.HttpUtility]::ParseQueryString($request.Url.Query)
      $path = $request.Url.AbsolutePath.Trim("/").ToLowerInvariant()

      if ($path -eq "health") {
        Write-JsonResponse $response 200 @{ ok = $true; port = $Port }
        $response.Close()
        continue
      }

      if ($path -eq "browse") {
        $root = $query["root"]
        $relativePath = $query["path"]
        $currentFolder = Resolve-SafePath -Root $root -RelativePath $relativePath

        if (-not (Test-Path -LiteralPath $currentFolder -PathType Container)) {
          throw "Requested folder does not exist."
        }

        $folderItems = Get-ChildItem -LiteralPath $root -Directory | Sort-Object Name
        $fileItems = Get-ChildItem -LiteralPath $currentFolder -File -Filter *.txt | Sort-Object Name

        $payload = @{
          root = (Normalize-WindowsPath $root)
          currentPath = $currentFolder
          currentRelativePath = (Get-RelativePath -Root $root -FullPath $currentFolder)
          folders = @($folderItems | ForEach-Object {
            @{
              name = $_.Name
              relativePath = (Get-RelativePath -Root $root -FullPath $_.FullName)
            }
          })
          files = @($fileItems | ForEach-Object {
            @{
              name = $_.Name
              fullPath = $_.FullName
            }
          })
        }

        $response.Headers["Cache-Control"] = "public, max-age=10"
        Write-JsonResponse $response 200 $payload
        $response.Close()
        continue
      }

      if ($path -eq "text") {
        $targetPath = Normalize-WindowsPath $query["path"]
        if ([string]::IsNullOrWhiteSpace($targetPath)) {
          throw "Text file path is empty."
        }
        if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
          throw "Text file does not exist."
        }

        $fileText = [System.IO.File]::ReadAllText($targetPath, [System.Text.Encoding]::UTF8)
        $response.Headers["Cache-Control"] = "public, max-age=300"
        Write-TextResponse $response 200 $fileText
        $response.Close()
        continue
      }

      if ($path -eq "proxy") {
        $clientKey = Get-ClientKey $request
        $fallbackTarget = ""
        $existingTarget = $null
        if ($script:LastTargetByClient.TryGetValue($clientKey, [ref]$existingTarget)) {
          $fallbackTarget = [string]$existingTarget
        }
        $targetUri = Get-TargetUri -Value $query["target"] -Referrer $request.UrlReferrer -FallbackTarget $fallbackTarget
        $script:LastTargetByClient[$clientKey] = $targetUri.AbsoluteUri

        $cacheKey = if ($request.HttpMethod -eq "GET") { $targetUri.AbsoluteUri } else { "" }
        $cachedPayload = Try-GetCachedProxyPayload -CacheKey $cacheKey
        if ($cachedPayload) {
          Write-ProxiedPayload -Response $response -Payload $cachedPayload
          $response.Close()
          return
        }

        $cookieJarPath = Get-CookieJarPath -ClientKey $clientKey
        $upstreamReferrer = Get-UpstreamReferrer -Referrer $request.UrlReferrer
        $upstreamResponse = Invoke-CurlProxyRequest -TargetUri $targetUri -Request $request -CookieJarPath $cookieJarPath -UpstreamReferrer $upstreamReferrer
        $finalUri = if ($upstreamResponse.EffectiveUri) { $upstreamResponse.EffectiveUri } else { $targetUri }
        $autoForwardTarget = Get-AutoForwardTarget -FinalUri $finalUri -UpstreamResponse $upstreamResponse
        if ($autoForwardTarget) {
          $script:LastTargetByClient[$clientKey] = $autoForwardTarget.AbsoluteUri
          $upstreamResponse = Invoke-CurlProxyRequest -TargetUri $autoForwardTarget -Request $request -CookieJarPath $cookieJarPath -UpstreamReferrer $finalUri.AbsoluteUri
          $finalUri = if ($upstreamResponse.EffectiveUri) { $upstreamResponse.EffectiveUri } else { $autoForwardTarget }
        }
        $script:LastTargetByClient[$clientKey] = $finalUri.AbsoluteUri
        $payload = Build-ProxiedPayload -UpstreamResponse $upstreamResponse -FinalUri $finalUri -PortNumber $Port
        $lifetimeSeconds = Get-CacheLifetimeSeconds -MediaType $payload.MediaType -FinalUri $finalUri
        $payload.CacheSeconds = $lifetimeSeconds
        Write-ProxiedPayload -Response $response -Payload $payload
        if ($request.HttpMethod -eq "GET" -and $lifetimeSeconds -gt 0) {
          Set-CachedProxyPayload -CacheKey $cacheKey -Payload $payload -LifetimeSeconds $lifetimeSeconds
        }
        $response.Close()
        continue
      }

      Write-JsonResponse $response 404 @{ error = "Not found." }
      $response.Close()
  } catch {
    Write-JsonResponse $response 500 @{ error = $_.Exception.Message }
    $response.Close()
  }
}

$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()

Write-Host "DeviceWebUI local server running on http://127.0.0.1:$Port/"

try {
  while ($listener.IsListening) {
    $context = $listener.GetContext()
    Process-RequestContext -Context $context
  }
} finally {
  if ($listener.IsListening) {
    $listener.Stop()
  }
  $listener.Close()
}
