import secrets


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        nonce = secrets.token_urlsafe(18)
        request.csp_nonce = nonce
        response = self.get_response(request)
        if not request.path.startswith("/admin/"):
            response.setdefault(
                "Content-Security-Policy",
                "; ".join(
                    (
                        "default-src 'self'",
                        "base-uri 'self'",
                        "object-src 'none'",
                        "frame-ancestors 'none'",
                        "form-action 'self'",
                        "img-src 'self' data:",
                        "font-src 'self' data:",
                        "connect-src 'self'",
                        f"script-src 'self' 'nonce-{nonce}'",
                        f"style-src 'self' 'nonce-{nonce}'",
                    )
                ),
            )
        return response
