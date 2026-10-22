"""Security headers middleware and Content Security Policy (CSP) for Aasaan.

Replicates and extends security headers previously set by Caddy for direct
container execution on Azure Container Apps (ACA) or local development.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("aasaan.security")

# Content Security Policy directives
CSP_DIRECTIVES = [
    "default-src 'self'",
    # 'unsafe-inline' is required for the inline theme loader script in index.html (lines 10-13)
    "script-src 'self' 'unsafe-inline'",
    # 'unsafe-inline' is required for dynamic inline style properties set by UI components (e.g. progress bars)
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    # Image sources: self, data:, blob: (attachments preview), Wikimedia Commons figures & thumbnails,
    # Openverse figures, YouTube video thumbnails, and Creative Commons providers
    "img-src 'self' data: blob: https://upload.wikimedia.org https://commons.wikimedia.org https://*.wikimedia.org https://api.openverse.org https://*.openverse.org https://i.ytimg.com https://*.ytimg.com https://img.youtube.com https://live.staticflickr.com https://*.staticflickr.com https://*.wp.com https://wordpress.org",
    # Connect/fetch/SSE endpoints (API, static files, and CSP report collector)
    "connect-src 'self'",
    # Frames: Embedded YouTube player (standard and privacy-enhanced domain)
    "frame-src 'self' https://www.youtube-nocookie.com https://www.youtube.com",
    # Matching Caddy's X-Frame-Options: SAMEORIGIN
    "frame-ancestors 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "report-uri /csp-report",
]

CSP_POLICY = "; ".join(CSP_DIRECTIVES)


def get_csp_mode() -> str:
    """Return active CSP mode: 'report-only' (default), 'enforce', or 'off'."""
    mode = os.environ.get("AASAAN_CSP_MODE", "report-only").strip().lower()
    return mode if mode in ("report-only", "enforce", "off") else "report-only"


class SecurityHeadersMiddleware:
    """ASGI middleware applying standard HTTP security headers and CSP to all responses.

    - Applies to all status codes (200, 404, 500) and static file transfers.
    - Operates purely on header events without buffering response bodies or disrupting SSE streams.
    - Does not overwrite headers already deliberately set by endpoint handlers.
    - Sends Strict-Transport-Security only when requests arrive over HTTPS (via X-Forwarded-Proto).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_in = Headers(scope=scope)
        proto = headers_in.get("x-forwarded-proto", scope.get("scheme", "http")).lower()
        is_https = proto == "https"
        response_started = False

        async def send_with_security_headers(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)

                # 1. Suppress Server header if present
                if "server" in headers:
                    del headers["server"]

                # 2. Replicate standard Caddy headers
                if "x-content-type-options" not in headers:
                    headers["X-Content-Type-Options"] = "nosniff"

                if "referrer-policy" not in headers:
                    headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

                if "x-frame-options" not in headers:
                    headers["X-Frame-Options"] = "SAMEORIGIN"

                # 3. Permissions Policy: conservative deny for unused hardware sensors/APIs
                if "permissions-policy" not in headers:
                    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

                # 4. Strict-Transport-Security: only over HTTPS so local dev is not poisoned
                if is_https and "strict-transport-security" not in headers:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

                # 5. Content-Security-Policy (mode controlled by AASAAN_CSP_MODE)
                csp_mode = get_csp_mode()
                if csp_mode != "off":
                    csp_header_name = (
                        "Content-Security-Policy"
                        if csp_mode == "enforce"
                        else "Content-Security-Policy-Report-Only"
                    )
                    # Do not overwrite if already explicitly set by route
                    if (
                        "content-security-policy" not in headers
                        and "content-security-policy-report-only" not in headers
                    ):
                        headers[csp_header_name] = CSP_POLICY

            await send(message)

        try:
            await self.app(scope, receive, send_with_security_headers)
        except Exception as exc:
            logger.error("Unhandled error during HTTP processing: %s", exc, exc_info=True)
            if not response_started:
                err_body = b'{"error":"internal_server_error"}'
                start_msg = {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                }
                await send_with_security_headers(start_msg)
                await send({
                    "type": "http.response.body",
                    "body": err_body,
                    "more_body": False,
                })
            else:
                raise
