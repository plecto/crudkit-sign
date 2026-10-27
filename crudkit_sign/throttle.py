"""Per-IP rate limiting for the public signer views, backed by the default cache."""

from django.core.cache import cache

from crudkit_sign.conf import get_setting

WINDOW_SECONDS = 60


def client_ip(request):
    """The client's IP, trusting only the last ``trusted_proxy_hops`` entries of X-Forwarded-For."""
    hops = get_setting("trusted_proxy_hops")
    if hops:
        forwarded = [
            value.strip() for value in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if value.strip()
        ]
        if forwarded:
            return forwarded[max(len(forwarded) - hops, 0)]
    return request.META.get("REMOTE_ADDR", "")


def allow(request):
    """False when this client has exceeded the per-minute limit."""
    key = f"crudkit_sign:throttle:{client_ip(request)}"
    added = cache.add(key, 1, WINDOW_SECONDS)
    if added:
        return True
    try:
        count = cache.incr(key)
    except ValueError:
        # Expired between add and incr.
        cache.add(key, 1, WINDOW_SECONDS)
        return True
    return count <= get_setting("throttle_per_minute")
