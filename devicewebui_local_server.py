import json
import mimetypes
import re
import ssl
import sys
import threading
import time
import gzip
import zlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape as html_escape
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
PROXY_REVISION = "20260319-ytfix2"
SSL_CONTEXT = ssl._create_unverified_context()
CACHE_LOCK = threading.Lock()
SESSION_LOCK = threading.Lock()
LAST_TARGET_BY_CLIENT: dict[str, str] = {}
PROXY_CACHE: dict[str, tuple[float, "Payload"]] = {}
CLIENT_OPENERS: dict[str, urllib.request.OpenerDirector] = {}


@dataclass
class UpstreamResponse:
    status_code: int
    headers: dict[str, list[str]]
    content_type: str
    media_type: str
    final_url: str
    body: bytes


@dataclass
class Payload:
    status_code: int
    content_type: str
    headers: dict[str, str]
    body: bytes
    media_type: str
    cache_seconds: int = 0


def decode_upstream_body(body: bytes, content_encoding: str) -> bytes:
    encoding = (content_encoding or "").lower().strip()
    if not encoding or not body:
        return body
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        return zlib.decompress(body)
    if "br" in encoding:
        try:
            import brotli  # type: ignore
            return brotli.decompress(body)
        except Exception:
            return body
    return body


def normalize_windows_path(value: str) -> str:
    if not value:
        return ""
    normalized = str(value).strip().replace("/", "\\")
    while normalized.endswith("\\"):
        normalized = normalized[:-1]
    return normalized


def normalize_relative_path(value: str) -> str:
    return str(value or "").strip().replace("/", "\\").lstrip("\\")


def normalize_target_url(value: str) -> str:
    candidate = (value or "").replace("\ufeff", "").strip()
    if not candidate:
        return ""
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = "https://" + candidate
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return candidate


def resolve_safe_path(root: str, relative_path: str) -> Path:
    normalized_root = normalize_windows_path(root)
    if not normalized_root:
        raise ValueError("Root folder is empty.")
    root_path = Path(normalized_root).resolve()
    if not root_path.is_dir():
        raise ValueError("Root folder does not exist.")

    if normalize_relative_path(relative_path):
        combined = (root_path / normalize_relative_path(relative_path)).resolve()
    else:
        combined = root_path

    if combined != root_path and root_path not in combined.parents:
        raise ValueError("Requested path is outside the root folder.")
    return combined


def get_relative_path(root: str, full_path: str) -> str:
    root_path = Path(normalize_windows_path(root)).resolve()
    full = Path(normalize_windows_path(full_path)).resolve()
    if full == root_path:
        return ""
    try:
        return str(full.relative_to(root_path)).replace("/", "\\")
    except ValueError:
        return ""


def get_proxy_url(target_url: str, session_id: str = "") -> str:
    query_parts = [
        ("target", target_url),
        ("rev", PROXY_REVISION),
    ]
    if session_id:
        query_parts.append(("sid", session_id))
    return f"http://127.0.0.1:{PORT}/proxy?{urllib.parse.urlencode(query_parts)}"


def get_runtime_proxy_prefix(target_root_url: str, session_id: str = "") -> str:
    query_parts = []
    if session_id:
        query_parts.append(("sid", session_id))
    query_parts.append(("target", target_root_url))
    return f"http://127.0.0.1:{PORT}/proxy?{urllib.parse.urlencode(query_parts)}"


def get_client_key(handler: BaseHTTPRequestHandler) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", handler.client_address[0] if handler.client_address else "unknown")


def get_target_url(raw_value: str, referrer: str, fallback_target: str) -> str:
    candidate = (raw_value or "").strip()
    if not candidate and referrer:
        ref_query = urllib.parse.parse_qs(urllib.parse.urlparse(referrer).query)
        candidate = (ref_query.get("target", [""])[0] or "").strip()
    if not candidate and fallback_target:
        candidate = fallback_target.strip()
    if not candidate:
        raise ValueError("Target URL is empty.")
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = "https://" + candidate
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Target URL is invalid.")
    return candidate


def get_fallback_target_url(request_path: str, request_query: str, referrer: str, fallback_target: str) -> str:
    ref_target = ""
    if referrer:
        ref_query = urllib.parse.parse_qs(urllib.parse.urlparse(referrer).query)
        ref_target = (ref_query.get("target", [""])[0] or "").strip()
    base_target = ref_target or (fallback_target or "").strip()
    if not base_target:
        raise ValueError("Target URL is empty.")
    clean_query = urllib.parse.parse_qs(request_query, keep_blank_values=True)
    clean_query.pop("rev", None)
    query_string = urllib.parse.urlencode(clean_query, doseq=True)
    relative_path = request_path or "/"
    if query_string:
        relative_path = f"{relative_path}?{query_string}"
    return urllib.parse.urljoin(base_target, relative_path)


def read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    transfer_encoding = (handler.headers.get("Transfer-Encoding", "") or "").lower()
    if "chunked" in transfer_encoding:
        chunks: list[bytes] = []
        while True:
            size_line = handler.rfile.readline()
            if not size_line:
                break
            size_text = size_line.strip().split(b";", 1)[0]
            if not size_text:
                continue
            chunk_size = int(size_text, 16)
            if chunk_size == 0:
                while True:
                    trailer = handler.rfile.readline()
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunk = handler.rfile.read(chunk_size)
            if chunk:
                chunks.append(chunk)
            handler.rfile.read(2)
        return b"".join(chunks)

    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    return handler.rfile.read(content_length) if content_length > 0 else b""


def resolve_text_file_url(file_path: Path) -> str:
    text = file_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        normalized = normalize_target_url(line)
        if normalized:
            return normalized
    return ""


def parse_text_file_metadata(file_path: Path) -> dict[str, str]:
    text = file_path.read_text(encoding="utf-8")
    lines = [line.replace("\ufeff", "").strip() for line in text.splitlines()]
    non_empty_lines = [line for line in lines if line]

    resolved_url = ""
    if non_empty_lines:
        resolved_url = normalize_target_url(non_empty_lines[0])

    icon_name = ""
    if len(non_empty_lines) >= 2:
        icon_name = non_empty_lines[1]

    return {
        "url": resolved_url,
        "iconName": icon_name,
    }


def resolve_icon_file(root: str, icon_name: str) -> Path | None:
    candidate_name = str(icon_name or "").replace("\ufeff", "").strip().replace("/", "\\").lstrip("\\")
    candidate_name = re.sub(r"(?i)^icon\s*[:=]\s*", "", candidate_name).strip()
    candidate_name = re.sub(r"(?i)^icons\\+", "", candidate_name).strip()
    if not candidate_name:
        return None

    try:
        icons_root = resolve_safe_path(root, "Icons")
    except Exception:
        return None

    if not icons_root.is_dir():
        return None

    possible_relative_paths: list[str] = [candidate_name]
    if not Path(candidate_name).suffix:
        possible_relative_paths.extend([
            candidate_name + ".png",
            candidate_name + ".svg",
            candidate_name + ".jpg",
            candidate_name + ".jpeg",
            candidate_name + ".gif",
            candidate_name + ".webp",
            candidate_name + ".bmp",
            candidate_name + ".ico",
        ])

    for relative_path in possible_relative_paths:
        try:
            icon_path = resolve_safe_path(root, "Icons\\" + normalize_relative_path(relative_path))
        except Exception:
            continue
        if icon_path.is_file():
            return icon_path

    lowered_target = candidate_name.lower()
    for item in icons_root.rglob("*"):
        if not item.is_file():
            continue
        stem_match = item.stem.lower() == lowered_target
        name_match = item.name.lower() == lowered_target
        relative_match = str(item.relative_to(icons_root)).replace("/", "\\").lower() == lowered_target
        if stem_match or name_match or relative_match:
            return item

    return None


def build_icon_url(root: str, icon_name: str) -> str:
    icon_path = resolve_icon_file(root, icon_name)
    if not icon_path:
        return ""
    query = urllib.parse.urlencode({
        "root": normalize_windows_path(root),
        "name": icon_name,
    })
    return f"http://127.0.0.1:{PORT}/icon?{query}"


def get_cache_lifetime_seconds(media_type: str, final_url: str) -> int:
    path = urllib.parse.urlparse(final_url).path if final_url else ""
    lowered_media = (media_type or "").lower()
    if lowered_media.startswith("text/html"):
        return 10
    if (
        lowered_media.startswith("text/css")
        or lowered_media.startswith("application/javascript")
        or lowered_media.startswith("text/javascript")
        or lowered_media.startswith("application/x-javascript")
        or lowered_media.startswith("image/")
        or lowered_media.startswith("font/")
    ):
        return 3600
    if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|eot)$", path, re.IGNORECASE):
        return 3600
    return 0


def try_get_cached_payload(cache_key: str) -> Payload | None:
    if not cache_key:
        return None
    with CACHE_LOCK:
        entry = PROXY_CACHE.get(cache_key)
        if not entry:
            return None
        expires_at, payload = entry
        if expires_at <= time.time():
            PROXY_CACHE.pop(cache_key, None)
            return None
        return payload


def set_cached_payload(cache_key: str, payload: Payload, lifetime_seconds: int) -> None:
    if not cache_key or not payload or lifetime_seconds <= 0:
        return
    with CACHE_LOCK:
        PROXY_CACHE[cache_key] = (time.time() + lifetime_seconds, payload)


def get_client_opener(client_key: str) -> urllib.request.OpenerDirector:
    with SESSION_LOCK:
        opener = CLIENT_OPENERS.get(client_key)
        if opener:
            return opener
        cookie_jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=SSL_CONTEXT),
            urllib.request.HTTPHandler(),
        )
        CLIENT_OPENERS[client_key] = opener
        return opener


def get_header_case_insensitive(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def maybe_proxify_script_path(raw_value: str, base_url: str, session_id: str = "") -> str:
    candidate = (raw_value or "").strip()
    if not candidate:
        return raw_value
    lowered = candidate.lower()
    if lowered.startswith(("http://127.0.0.1:", "data:", "javascript:", "mailto:", "tel:", "#")):
        return raw_value
    if candidate in ("/", "./", "../"):
        return raw_value
    if candidate.startswith(".") and "/" not in candidate and "\\" not in candidate:
        return raw_value
    has_path_separator = "/" in candidate or "\\" in candidate
    bare_filename = not has_path_separator and not re.match(r"^[a-z][a-z0-9+.-]*:", lowered, re.IGNORECASE)
    if bare_filename and re.search(r"\.(png|jpg|jpeg|gif|svg|ico|webp|bmp|woff2?|ttf|eot|css|js)(\?|$)", lowered):
        return raw_value
    should_proxy = False
    if re.match(r"^https?://", candidate, re.IGNORECASE):
        try:
            parsed = urllib.parse.urlparse(candidate)
            host = (parsed.netloc or "").lower()
            if any(
                host == allowed_host or host.endswith("." + allowed_host)
                for allowed_host in (
                    "youtube.com",
                    "youtu.be",
                    "consent.youtube.com",
                    "accounts.google.com",
                    "policies.google.com",
                    "support.google.com",
                    "google.com",
                )
            ):
                should_proxy = True
        except Exception:
            pass
    if re.search(r"\.(mwsl|gif|png|jpg|jpeg|svg|ico|css|js)(\?|$)", lowered):
        should_proxy = True
    if any(token in candidate for token in ("Portal/", "Images/", "Scripts/", "CSS/", "ClientArea/", "cpu/")):
        should_proxy = True
    if not should_proxy:
        return raw_value
    try:
        absolute = urllib.parse.urljoin(base_url, candidate)
        return get_proxy_url(absolute, session_id)
    except Exception:
        return raw_value


def rewrite_javascript_content(script: str, base_url: str = "", session_id: str = "") -> str:
    replacements = (
        (r"window\.top\.document", "document"),
        (r"top\.window\.document", "document"),
        (r"top\.document", "document"),
        (r"window\.top\.location", "window.location"),
        (r"top\.window\.location", "window.location"),
        (r"top\.location", "window.location"),
        (r"top\.server_frame", "window.server_frame"),
        (r"parent\.document", "document"),
        (r"parent\.frames", "window.frames"),
        (r"parent\.\$", "window.$"),
        (r"parent\.leds", "window.leds"),
        (r"parent\.update_interval", "window.update_interval"),
        (r"parent\.update_phrase_on", "window.update_phrase_on"),
        (r"parent\.update_phrase_off", "window.update_phrase_off"),
        (r"parent\.start_update", "window.start_update"),
        (r"parent\.UpdatePageDataManager", "window.UpdatePageDataManager"),
        (r"parent\.sortables_init_fb_update", "window.sortables_init_fb_update"),
        (r"frames\.server_frame", "window.frames.server_frame"),
        (r"top\.doUpdate", "window.doUpdate"),
        (r"window\.location\.replace\s*\(", "window.__devicewebuiReplace("),
        (r"window\.location\.assign\s*\(", "window.__devicewebuiAssign("),
        (r"(?<![\w.])location\.replace\s*\(", "window.__devicewebuiReplace("),
        (r"(?<![\w.])location\.assign\s*\(", "window.__devicewebuiAssign("),
    )
    rewritten = script
    for pattern, replacement in replacements:
        rewritten = re.sub(pattern, replacement, rewritten)

    if base_url:
        proxied_root = get_runtime_proxy_prefix(urllib.parse.urljoin(base_url, "/"), session_id)
        rewritten = rewritten.replace(
            'e=e.replace(/#.*$/,"").replace(/\\?.*$/,"").replace(/\\/[^\\/]+$/,"/"),i.p=e',
            f'e="{proxied_root}",i.p=e',
        )

    if base_url:
        def rewrite_string_literal(match: re.Match[str]) -> str:
            quote = match.group(1)
            raw = match.group(2)
            rewritten_value = maybe_proxify_script_path(raw, base_url, session_id)
            return f"{quote}{rewritten_value}{quote}"

        rewritten = re.sub(r"""(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1""", rewrite_string_literal, rewritten)
    return rewritten


def rewrite_css_content(css: str, final_url: str, session_id: str = "") -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(2).strip()
        if not raw or raw.startswith("data:") or raw.startswith("javascript:") or raw.startswith("#"):
            return match.group(0)
        try:
            absolute = urllib.parse.urljoin(final_url, raw)
            return f'url("{get_proxy_url(absolute, session_id)}")'
        except Exception:
            return match.group(0)

    return re.sub(r"""(?i)url\(\s*(['"]?)([^)'"]+)\1\s*\)""", repl, css)


def build_proxy_script(base_href: str, session_id: str = "") -> str:
    proxy_root = f"http://127.0.0.1:{PORT}/proxy?"
    query_parts = []
    if session_id:
        query_parts.append(f"sid={urllib.parse.quote(session_id, safe='')}")
    query_prefix = ("&".join(query_parts) + "&") if query_parts else ""
    return f"""
<script>
(function () {{
  var proxyRoot = {json.dumps(proxy_root)};
  var proxyQueryPrefix = {json.dumps(query_prefix)};
  var proxyPrefixPattern = /^http:\\/\\/127\\.0\\.0\\.1:{PORT}\\/proxy\\?(?:[^#]*&)?target=|^http:\\/\\/127\\.0\\.0\\.1:{PORT}\\/proxy\\?(?:[^#]*&)?sid=[^#&]+(?:&[^#]*)?&target=/i;
  var currentBase = {json.dumps(base_href)};
  var localOrigin = "http://127.0.0.1:{PORT}";
  var currentBaseUrl = null;
  var currentOrigin = "";
  try {{
    currentBaseUrl = new URL(currentBase);
    currentOrigin = currentBaseUrl.origin || "";
  }} catch (_error) {{}}
  function toAbsolute(input) {{
    try {{ return new URL(input, currentBase).toString(); }} catch (_error) {{ return input; }}
  }}
  function proxify(input) {{
    if (typeof input !== 'string') {{ return input; }}
      if (!input) {{ return input; }}
      if (proxyPrefixPattern.test(input)) {{ return input; }}
      var absolute = toAbsolute(input);
      if (!absolute) {{ return input; }}
      if (/^http:\/\/127\.0\.0\.1:{PORT}\/proxy(?:[?#].*)?$/i.test(absolute)) {{
        return proxyRoot + proxyQueryPrefix + "target=" + encodeURIComponent(currentBase);
      }}
      if (currentOrigin && absolute.indexOf(localOrigin + "/") === 0 && !/^http:\/\/127\.0\.0\.1:{PORT}\/proxy[/?#]?/i.test(absolute)) {{
        absolute = currentOrigin + absolute.substring(localOrigin.length);
      }}
      if (proxyPrefixPattern.test(absolute)) {{ return absolute; }}
      if (/^(mailto:|tel:|javascript:|data:|#)/i.test(absolute)) {{ return input; }}
      return proxyRoot + proxyQueryPrefix + "target=" + encodeURIComponent(absolute);
    }}
  function decodeEscapedUrl(input) {{
    if (typeof input !== 'string') {{ return input; }}
    return input
      .replace(/\\\\u0026/gi, '&')
      .replace(/\\u0026/gi, '&')
      .replace(/\\\\\\//g, '/')
      .replace(/\\\//g, '/');
  }}
  function normalizeText(input) {{
    return (input || '')
      .toString()
      .toLowerCase()
      .replace(/\\s+/g, ' ')
      .trim();
  }}
  var pageHtml = '';
  try {{
    pageHtml = document.documentElement ? (document.documentElement.innerHTML || '') : '';
  }} catch (_error) {{}}
  var consentSaveUrl = '';
  var consentDialogUrl = '';
  try {{
    var saveMatch = pageHtml.match(/"savePreferenceUrl":"([^"]+)"/i);
    if (saveMatch && saveMatch[1]) {{
      consentSaveUrl = decodeEscapedUrl(saveMatch[1]);
    }}
    var dialogMatch = pageHtml.match(/"url":"(https:\\/\\/consent\\.youtube\\.com\\/d[^"]+)"/i);
    if (dialogMatch && dialogMatch[1]) {{
      consentDialogUrl = decodeEscapedUrl(dialogMatch[1]);
    }}
  }} catch (_error) {{}}
    document.addEventListener('click', function (event) {{
      var clickable = event.target.closest('button, a, tp-yt-paper-button, yt-button-view-model button, [role=\"button\"]');
      if (clickable) {{
        var buttonText = normalizeText(clickable.innerText || clickable.textContent || clickable.getAttribute('aria-label'));
      if (buttonText) {{
        if ((buttonText.indexOf('prihvati sve') !== -1 || buttonText.indexOf('accept all') !== -1) && consentSaveUrl) {{
          event.preventDefault();
          event.stopPropagation();
          window.location.href = proxify(consentSaveUrl);
          return;
        }}
        if ((buttonText.indexOf('više opcija') !== -1 || buttonText.indexOf('more options') !== -1 || buttonText.indexOf('customize') !== -1) && consentDialogUrl) {{
          event.preventDefault();
          event.stopPropagation();
          window.location.href = proxify(consentDialogUrl);
          return;
        }}
        }}
      }}
      var anchor = event.target.closest('a[href]');
      var href = '';
      if (anchor) {{
        href = anchor.href || anchor.getAttribute('href') || '';
      }} else if (clickable) {{
        href = clickable.getAttribute('data-href') || clickable.getAttribute('data-url') || '';
      }}
      if (!href || href[0] === '#') {{ return; }}
      event.preventDefault();
      event.stopPropagation();
      if (typeof event.stopImmediatePropagation === 'function') {{
        event.stopImmediatePropagation();
      }}
      window.location.href = proxify(href);
    }}, true);
    document.addEventListener('submit', function (event) {{
      var form = event.target;
      if (!form) {{ return; }}
      var rawAction = form.getAttribute('action') || form.action;
      if (!rawAction) {{ return; }}
      form.action = proxify(rawAction);
    }}, true);
    if (window.history && window.history.pushState) {{
      var originalPushState = window.history.pushState.bind(window.history);
      window.history.pushState = function (state, title, url) {{
        if (typeof url === 'string' && url) {{
          arguments[2] = proxify(url);
        }}
        return originalPushState.apply(this, arguments);
      }};
    }}
    if (window.history && window.history.replaceState) {{
      var originalReplaceState = window.history.replaceState.bind(window.history);
      window.history.replaceState = function (state, title, url) {{
        if (typeof url === 'string' && url) {{
          arguments[2] = proxify(url);
        }}
        return originalReplaceState.apply(this, arguments);
      }};
    }}
    if (navigator.serviceWorker && navigator.serviceWorker.register) {{
      navigator.serviceWorker.register = function () {{
        return Promise.resolve();
      }};
    }}
    var originalFetch = window.fetch;
    if (originalFetch) {{
      window.fetch = function (input, init) {{
        if (typeof input === 'string') {{
          input = proxify(input);
      }} else if (input && input.url) {{
        input = proxify(input.url);
      }}
      return originalFetch.call(this, input, init);
    }};
  }}
  if (window.XMLHttpRequest) {{
    var originalOpen = window.XMLHttpRequest.prototype.open;
    window.XMLHttpRequest.prototype.open = function (method, url) {{
      if (typeof url === 'string') {{
        arguments[1] = proxify(url);
      }}
      return originalOpen.apply(this, arguments);
    }};
  }}
  if (window.WebSocket) {{
    var OriginalWebSocket = window.WebSocket;
    window.WebSocket = function (url, protocols) {{
      var absolute = toAbsolute(url);
      return protocols ? new OriginalWebSocket(absolute, protocols) : new OriginalWebSocket(absolute);
    }};
    window.WebSocket.prototype = OriginalWebSocket.prototype;
  }}
  var originalAssign = window.location.assign ? window.location.assign.bind(window.location) : null;
  window.__devicewebuiAssign = function (url) {{
    var nextUrl = proxify(url);
    if (originalAssign) {{
      originalAssign(nextUrl);
      return;
    }}
    window.location.href = nextUrl;
  }};
  var originalReplace = window.location.replace ? window.location.replace.bind(window.location) : null;
  window.__devicewebuiReplace = function (url) {{
    var nextUrl = proxify(url);
    if (originalReplace) {{
      originalReplace(nextUrl);
      return;
    }}
    window.location.href = nextUrl;
  }};
  var originalOpenWindow = window.open;
  if (originalOpenWindow) {{
    window.open = function (url, target, features) {{
      if (typeof url === 'string') {{
        url = proxify(url);
      }}
      if (url) {{
        window.location.href = url;
      }}
      return null;
    }};
  }}
}})();
</script>
"""


def rewrite_html_content(html: str, final_url: str, session_id: str = "") -> str:
    base_href = final_url.split("#", 1)[0]
    parsed_final = urllib.parse.urlparse(final_url)
    normalized_path = (parsed_final.path or "/").lower()
    if normalized_path in {"/", "/default.mwsl"} and re.search(r"(?i)Portal/Intro\.mwsl", html):
        html = re.sub(
            r"""(?i)(<meta[^>]+http-equiv\s*=\s*["']refresh["'][^>]+content\s*=\s*["'])\s*\d+\s*;\s*url\s*=\s*([^"' >]+)(["'][^>]*>)""",
            r"\g<1>0; URL=./Portal/Portal.mwsl?PriNav=Start&coming_from_intro=true\3",
            html,
            count=1,
        )
        html = re.sub(
            r"""(?i)href=(["'])\.\/Portal\/Intro\.mwsl\1""",
            r'href="./Portal/Portal.mwsl?PriNav=Start&amp;coming_from_intro=true"',
            html,
        )
    without_meta_csp = re.sub(
        r"""<meta[^>]+http-equiv\s*=\s*["']Content-Security-Policy["'][^>]*>""",
        "",
        html,
        flags=re.IGNORECASE,
    )

    if re.search(r"(?i)<head[^>]*>", without_meta_csp):
        without_meta_csp = re.sub(
            r"(?i)<head([^>]*)>",
            lambda m: f'<head{m.group(1)}><base href="{html_escape(base_href, quote=True)}">',
            without_meta_csp,
            count=1,
        )

    script_blocks: list[str] = []

    def replace_inline_script(match: re.Match[str]) -> str:
        attributes = match.group(1)
        body = match.group(2)
        if re.search(r"(?i)\bsrc\s*=", attributes):
            return match.group(0)
        rewritten_body = rewrite_javascript_content(body, final_url, session_id)
        placeholder = f"__DEVICEWEBUI_SCRIPT_BLOCK_{len(script_blocks)}__"
        script_blocks.append(f"<script{attributes}>{rewritten_body}</script>")
        return placeholder

    without_inline_scripts = re.sub(
        r"(?is)<script\b([^>]*)>(.*?)</script>",
        replace_inline_script,
        without_meta_csp,
    )

    def rewrite_attr(match: re.Match[str]) -> str:
        attribute = match.group(1)
        raw_value = match.group(3) if match.group(3) is not None else match.group(4)
        if not raw_value:
            return match.group(0)
        lowered = raw_value.lower()
        if raw_value.startswith("#") or lowered.startswith(("javascript:", "data:", "mailto:", "tel:")):
            return match.group(0)
        try:
            absolute = urllib.parse.urljoin(final_url, raw_value)
            proxy_url = get_proxy_url(absolute, session_id)
            return f'{attribute}="{html_escape(proxy_url, quote=True)}"'
        except Exception:
            return match.group(0)

    rewritten = re.sub(
        r"""(?i)\b(href|src|action)=("([^"]*)"|'([^']*)')""",
        rewrite_attr,
        without_inline_scripts,
    )

    def rewrite_refresh(match: re.Match[str]) -> str:
        prefix, content_value, suffix = match.group(1), match.group(2), match.group(3)
        parsed = re.match(r"^\s*(\d+)\s*;\s*url\s*=\s*(.+)\s*$", content_value, flags=re.IGNORECASE)
        if not parsed:
            return match.group(0)
        delay, target_part = parsed.group(1), parsed.group(2).strip().strip('"').strip("'")
        try:
            absolute = urllib.parse.urljoin(final_url, target_part)
            proxy_url = get_proxy_url(absolute, session_id)
            return f"<meta{prefix}{delay}; URL={html_escape(proxy_url, quote=True)}{suffix}>"
        except Exception:
            return match.group(0)

    rewritten = re.sub(
        r"""(?i)<meta([^>]+http-equiv\s*=\s*["']refresh["'][^>]+content\s*=\s*["'])([^"' >]+)(["'][^>]*)>""",
        rewrite_refresh,
        rewritten,
    )

    for idx, block in enumerate(script_blocks):
        rewritten = rewritten.replace(f"__DEVICEWEBUI_SCRIPT_BLOCK_{idx}__", block)

    def rewrite_remaining_absolute_url(match: re.Match[str]) -> str:
        quote = match.group(1)
        raw_value = match.group(2)
        rewritten_value = maybe_proxify_script_path(raw_value, final_url, session_id)
        return f"{quote}{rewritten_value}{quote}"

    rewritten = re.sub(
        r"""(["'])(https?://[^"'<>]+)\1""",
        rewrite_remaining_absolute_url,
        rewritten,
    )

    proxy_script = build_proxy_script(base_href, session_id)
    if re.search(r"(?i)</body>", rewritten):
        return re.sub(r"(?i)</body>", lambda _match: proxy_script + "</body>", rewritten, count=1)
    return rewritten + proxy_script


def build_proxied_payload(upstream: UpstreamResponse, final_url: str, session_id: str = "") -> Payload:
    headers: dict[str, str] = {}
    for key, values in upstream.headers.items():
        lowered = key.lower()
        if lowered in {
            "x-frame-options",
            "content-security-policy",
            "content-security-policy-report-only",
            "frame-options",
            "transfer-encoding",
            "content-length",
            "content-encoding",
        }:
            continue
        if lowered == "location":
            try:
                redirect_url = urllib.parse.urljoin(final_url, values[-1])
                headers["Location"] = get_proxy_url(redirect_url, session_id)
            except Exception:
                pass
            continue
        headers[key] = values[-1]

    media_type = upstream.media_type.lower()
    if media_type.startswith("text/html"):
        text = upstream.body.decode("utf-8", errors="replace")
        body = rewrite_html_content(text, final_url, session_id).encode("utf-8")
    elif media_type.startswith("text/css"):
        text = upstream.body.decode("utf-8", errors="replace")
        body = rewrite_css_content(text, final_url, session_id).encode("utf-8")
    elif media_type.startswith(("application/javascript", "text/javascript", "application/x-javascript")):
        text = upstream.body.decode("utf-8", errors="replace")
        body = rewrite_javascript_content(text, final_url, session_id).encode("utf-8")
    else:
        body = upstream.body

    return Payload(
        status_code=upstream.status_code,
        content_type=upstream.content_type,
        headers=headers,
        body=body,
        media_type=upstream.media_type,
    )


def get_auto_forward_target(final_url: str, upstream: UpstreamResponse) -> str | None:
    if not upstream.media_type.lower().startswith("text/html"):
        return None
    parsed = urllib.parse.urlparse(final_url)
    normalized_path = parsed.path.lower() or "/"
    html = upstream.body.decode("utf-8", errors="replace")
    if normalized_path in {"/", "/default.mwsl"}:
        if re.search(r"(?i)Portal/Intro\.mwsl", html):
            return urllib.parse.urljoin(final_url, "./Portal/Portal.mwsl?PriNav=Start&coming_from_intro=true")
    if not normalized_path.endswith("/portal/intro.mwsl"):
        return None
    if re.search(r'(?i)class="enterformclass"', html) and re.search(r"(?i)Portal\.mwsl", html):
        return urllib.parse.urljoin(final_url, "../Portal/Portal.mwsl?PriNav=Start&coming_from_intro=true")
    return None


def invoke_upstream_request(target_url: str, handler: BaseHTTPRequestHandler, client_key: str, upstream_referrer: str) -> UpstreamResponse:
    opener = get_client_opener(client_key)
    body = b""
    if handler.command not in {"GET", "HEAD"}:
        body = read_request_body(handler)

    request = urllib.request.Request(target_url, data=body or None, method=handler.command)
    target_parts = urllib.parse.urlparse(target_url)
    target_origin = f"{target_parts.scheme}://{target_parts.netloc}" if target_parts.scheme and target_parts.netloc else ""
    excluded_headers = {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "referer",
        "origin",
        "accept-encoding",
        "if-none-match",
        "if-modified-since",
        "if-match",
        "if-unmodified-since",
        "if-range",
        "cache-control",
        "pragma",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
    }
    saw_user_agent = False
    for header_name, header_value in handler.headers.items():
        lowered = header_name.lower()
        if lowered in excluded_headers:
            continue
        if lowered == "user-agent":
            saw_user_agent = True
        request.add_header(header_name, header_value)
    if not saw_user_agent:
        request.add_header("User-Agent", "DeviceWebUIProxy/2.0")
    if target_origin:
        request.add_header("Origin", target_origin)
    if upstream_referrer:
        request.add_header("Referer", upstream_referrer)

    try:
        response = opener.open(request, timeout=20)
        status_code = response.status
        final_url = response.geturl()
        raw_headers = response.info()
        body_bytes = response.read()
    except urllib.error.HTTPError as error:
        response = error
        status_code = error.code
        final_url = error.geturl()
        raw_headers = error.info()
        body_bytes = error.read()

    headers: dict[str, list[str]] = {}
    for key, value in raw_headers.items():
        headers.setdefault(key, []).append(value)
    content_type = get_header_case_insensitive({k: v[-1] for k, v in headers.items()}, "Content-Type") or "application/octet-stream"
    content_encoding = get_header_case_insensitive({k: v[-1] for k, v in headers.items()}, "Content-Encoding")
    media_type = content_type.split(";", 1)[0].strip()
    decoded_body = body_bytes
    if media_type.lower().startswith(("text/", "application/javascript", "text/javascript", "application/x-javascript", "application/json")):
        decoded_body = decode_upstream_body(body_bytes, content_encoding)
    return UpstreamResponse(
        status_code=status_code,
        headers=headers,
        content_type=content_type,
        media_type=media_type,
        final_url=final_url,
        body=decoded_body,
    )


class DeviceWebUIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _set_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status_code: int, text: str, cache_control: str = "") -> None:
        body = text.encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_payload(self, payload: Payload) -> None:
        self.send_response(payload.status_code)
        self._set_cors_headers()
        for key, value in payload.headers.items():
            lowered = key.lower()
            if lowered in {
                "x-frame-options",
                "content-security-policy",
                "content-security-policy-report-only",
                "frame-options",
                "transfer-encoding",
                "content-length",
                "connection",
                "server",
                "date",
                "cache-control",
            }:
                continue
            self.send_header(key, value)
        self.send_header("Content-Type", payload.content_type)
        if payload.cache_seconds > 0:
            self.send_header("Cache-Control", f"public, max-age={payload.cache_seconds}")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(payload.body)))
        self.end_headers()
        self.wfile.write(payload.body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._handle_request()

    def do_POST(self) -> None:
        self._handle_request()

    def _handle_request(self) -> None:
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            path = parsed_url.path.strip("/").lower()
            query = urllib.parse.parse_qs(parsed_url.query)
            client_key = get_client_key(self)
            session_id = (query.get("sid", [""])[0] or "").strip()
            session_key = f"{client_key}::{session_id}" if session_id else client_key

            if path == "health":
                self._send_json(200, {"ok": True, "port": PORT})
                return

            if path == "browse":
                root = query.get("root", [""])[0]
                relative_path = query.get("path", [""])[0]
                current_folder = resolve_safe_path(root, relative_path)
                if not current_folder.is_dir():
                    raise ValueError("Requested folder does not exist.")
                root_path = Path(normalize_windows_path(root)).resolve()
                folders = sorted(
                    [
                        item for item in root_path.iterdir()
                        if item.is_dir() and item.name.lower() != "icons"
                    ],
                    key=lambda item: item.name.lower()
                )
                files = sorted([item for item in current_folder.iterdir() if item.is_file() and item.suffix.lower() == ".txt"], key=lambda item: item.name.lower())
                payload = {
                    "root": normalize_windows_path(root),
                    "currentPath": str(current_folder),
                    "currentRelativePath": get_relative_path(root, str(current_folder)),
                    "folders": [{"name": item.name, "relativePath": get_relative_path(root, str(item))} for item in folders],
                    "files": [
                        {
                            "name": item.name,
                            "displayName": re.sub(r"\.txt$", "", item.name, flags=re.IGNORECASE),
                            "fullPath": str(item),
                            "iconName": parse_text_file_metadata(item).get("iconName", ""),
                            "iconUrl": build_icon_url(root, parse_text_file_metadata(item).get("iconName", "")),
                        }
                        for item in files
                    ],
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=10")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "text":
                target_path = Path(normalize_windows_path(query.get("path", [""])[0]))
                if not str(target_path):
                    raise ValueError("Text file path is empty.")
                if not target_path.is_file():
                    raise ValueError("Text file does not exist.")
                self._send_text(200, target_path.read_text(encoding="utf-8"), "public, max-age=300")
                return

            if path == "link":
                target_path = Path(normalize_windows_path(query.get("path", [""])[0]))
                if not str(target_path):
                    raise ValueError("Text file path is empty.")
                if not target_path.is_file():
                    raise ValueError("Text file does not exist.")
                metadata = parse_text_file_metadata(target_path)
                resolved_url = metadata.get("url", "")
                if not resolved_url:
                    raise ValueError("The text file does not contain a valid URL.")
                root_value = query.get("root", [""])[0]
                payload = {
                    "name": target_path.name,
                    "displayName": re.sub(r"\.txt$", "", target_path.name, flags=re.IGNORECASE),
                    "fullPath": str(target_path),
                    "url": resolved_url,
                    "iconName": metadata.get("iconName", ""),
                    "iconUrl": build_icon_url(root_value, metadata.get("iconName", "")) if root_value else "",
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "icon":
                root = query.get("root", [""])[0]
                icon_name = query.get("name", [""])[0]
                icon_path = resolve_icon_file(root, icon_name)
                if not icon_path or not icon_path.is_file():
                    raise ValueError("Icon file does not exist.")
                body = icon_path.read_bytes()
                content_type = mimetypes.guess_type(icon_path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "proxy":
                with SESSION_LOCK:
                    fallback_target = LAST_TARGET_BY_CLIENT.get(session_key, "")
                if self.command not in {"GET", "HEAD"} and not query.get("target", [""])[0]:
                    _ = read_request_body(self)
                    self.send_response(204)
                    self._set_cors_headers()
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                target_url = get_target_url(
                    query.get("target", [""])[0],
                    self.headers.get("Referer", ""),
                    fallback_target,
                )
                with SESSION_LOCK:
                    LAST_TARGET_BY_CLIENT[session_key] = target_url

                cache_key = f"{session_key}|{target_url}" if self.command == "GET" else ""
                cached = try_get_cached_payload(cache_key)
                if cached:
                    self._write_payload(cached)
                    return

                referrer = self.headers.get("Referer", "")
                upstream_referrer = ""
                if referrer:
                    ref_query = urllib.parse.parse_qs(urllib.parse.urlparse(referrer).query)
                    upstream_referrer = ref_query.get("target", [""])[0]

                upstream = invoke_upstream_request(target_url, self, session_key, upstream_referrer)
                final_url = upstream.final_url or target_url
                auto_forward_target = get_auto_forward_target(final_url, upstream)
                if auto_forward_target:
                    with SESSION_LOCK:
                        LAST_TARGET_BY_CLIENT[session_key] = auto_forward_target
                    upstream = invoke_upstream_request(auto_forward_target, self, session_key, final_url)
                    final_url = upstream.final_url or auto_forward_target

                with SESSION_LOCK:
                    LAST_TARGET_BY_CLIENT[session_key] = final_url

                payload = build_proxied_payload(upstream, final_url, session_id)
                payload.cache_seconds = get_cache_lifetime_seconds(payload.media_type, final_url)
                self._write_payload(payload)
                if self.command == "GET" and payload.cache_seconds > 0:
                    set_cached_payload(cache_key, payload, payload.cache_seconds)
                return

            if parsed_url.path and parsed_url.path != "/":
                with SESSION_LOCK:
                    fallback_target = LAST_TARGET_BY_CLIENT.get(session_key, "")
                target_url = get_fallback_target_url(
                    parsed_url.path,
                    parsed_url.query,
                    self.headers.get("Referer", ""),
                    fallback_target,
                )
                cache_key = f"{session_key}|{target_url}" if self.command == "GET" else ""
                cached = try_get_cached_payload(cache_key)
                if cached:
                    self._write_payload(cached)
                    return

                upstream_referrer = ""
                referrer = self.headers.get("Referer", "")
                if referrer:
                    ref_query = urllib.parse.parse_qs(urllib.parse.urlparse(referrer).query)
                    upstream_referrer = ref_query.get("target", [""])[0]

                upstream = invoke_upstream_request(target_url, self, session_key, upstream_referrer)
                final_url = upstream.final_url or target_url
                with SESSION_LOCK:
                    LAST_TARGET_BY_CLIENT[session_key] = final_url
                payload = build_proxied_payload(upstream, final_url, session_id)
                payload.cache_seconds = get_cache_lifetime_seconds(payload.media_type, final_url)
                self._write_payload(payload)
                if self.command == "GET" and payload.cache_seconds > 0:
                    set_cached_payload(cache_key, payload, payload.cache_seconds)
                return

            self._send_json(404, {"error": "Not found."})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DeviceWebUIHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
