"""StarBuild – Hotel Construction Management System: Django settings."""
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")


def _csv(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


ALLOWED_HOSTS = _csv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = _csv("DJANGO_CSRF_TRUSTED_ORIGINS")
# Render gives every service its own address (e.g. starbuild.onrender.com); allow it automatically.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    # StarBuild apps
    "core",
    "accounts",
    "projects",
    "inventory",
    "finance",
    "boq",
    "procurement",
    "workforce",
    "api",
    "fieldwork",
    "documents",
    "notifications",
    "reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

if os.environ.get("DATABASE_URL"):  # hosted PostgreSQL (Render, Railway ...)
    _db = urlparse(os.environ["DATABASE_URL"])
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(_db.path.lstrip("/")),
            "USER": unquote(_db.username or ""),
            "PASSWORD": unquote(_db.password or ""),
            "HOST": _db.hostname or "localhost",
            "PORT": str(_db.port or 5432),
            "CONN_MAX_AGE": 60,
        }
    }
elif os.environ.get("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ.get("DB_USER", ""),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
else:  # local development only
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Kampala"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))  # on Render: the persistent disk

# Photos and documents are kept in a private Cloudflare R2 bucket when R2_BUCKET is set (they are only ever
# served through the login-checked views). Without it they stay on the local / Render disk.
R2_BUCKET = os.environ.get("R2_BUCKET", "")
if R2_BUCKET:
    _r2 = {k: os.environ.get(k, "") for k in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")}
    _missing = [k for k, v in _r2.items() if not v]
    if _missing:
        raise ImproperlyConfigured("R2_BUCKET is set, so also set: " + ", ".join(_missing))
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": R2_BUCKET,
            "endpoint_url": _r2["R2_ENDPOINT_URL"],
            "access_key": _r2["R2_ACCESS_KEY_ID"],
            "secret_key": _r2["R2_SECRET_ACCESS_KEY"],
            "region_name": "auto",
            "signature_version": "s3v4",
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }

# Nightly backups: how many copies to keep (the oldest are deleted).
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))

# E-mail (alerts to people with approvals or problems waiting). Without EMAIL_HOST the messages are only printed.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "StarBuild <no-reply@taajpalm.com>")
SITE_URL = os.environ.get("SITE_URL", "https://taajpalm.com")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Security for the live site (HTTPS is terminated by the host's proxy) -----------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "3600"))  # raise to 31536000 once HTTPS is proven
    SECURE_REFERRER_POLICY = "same-origin"

# --- StarBuild business rules -------------------------------------------------
# Purchase orders above this value (UGX) need a user with the "approve_large_po"
# capability (Project Owner/Director or System Administrator).
# Advances to suppliers (payments beyond the value delivered)
ADVANCE_MAX_PERCENT = Decimal(os.environ.get("ADVANCE_MAX_PERCENT", "50"))  # of the PO value, above this the Owner/Admin must approve
MOBILISATION_MAX_PERCENT = Decimal(os.environ.get("MOBILISATION_MAX_PERCENT", "20"))  # contractor mobilisation advance, % of contract amount
WORKER_ADVANCE_MAX_DAYS = int(os.environ.get("WORKER_ADVANCE_MAX_DAYS", "26"))  # salary advance limit, days of wages
WORKER_ADVANCE_DEDUCTION_PERCENT = Decimal(os.environ.get("WORKER_ADVANCE_DEDUCTION_PERCENT", "50"))  # max share of a payroll period's pay recovered
ADVANCE_ALERT_DAYS = int(os.environ.get("ADVANCE_ALERT_DAYS", "30"))  # flag advances with goods still undelivered after this long
PO_APPROVAL_THRESHOLD_UGX = Decimal(os.environ.get("PO_APPROVAL_THRESHOLD_UGX", "5000000"))

# Supplier quotations required before a non-emergency requisition can become a purchase order.
MIN_QUOTATIONS = int(os.environ.get("MIN_QUOTATIONS", "1"))

# Wage rules: hours in a full working day, and the overtime pay multiplier.
STANDARD_WORK_HOURS = Decimal(os.environ.get("STANDARD_WORK_HOURS", "8"))
OVERTIME_MULTIPLIER = Decimal(os.environ.get("OVERTIME_MULTIPLIER", "1.5"))

# --- Production security --------------------------------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_CONTENT_TYPE_NOSNIFF = True

# ---- REST API (mobile app) ----
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"login": "30/min"},
}