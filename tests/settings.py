"""Minimal settings for running the addon's own tests on sqlite."""

import os
import tempfile

SECRET_KEY = "test"
DEBUG = True
ALLOWED_HOSTS = ["*"]
BASE_URL = "https://crm.example.com"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "crudkit",
    "crudkit_sign",
    "tests.testapp",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "tests.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ.get("CRUDKIT_SIGN_TEST_DB", ":memory:")}
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
MEDIA_ROOT = os.environ.get("CRUDKIT_SIGN_TEST_MEDIA") or tempfile.mkdtemp(prefix="crudkit-sign-test-")
MEDIA_URL = "/media/"
USE_TZ = True
TIME_ZONE = "UTC"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "sign@example.com"
CELERY_TASK_ALWAYS_EAGER = True
CRUDKIT_SIGN = {"brand_name": "Example Inc", "throttle_per_minute": 1000}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
