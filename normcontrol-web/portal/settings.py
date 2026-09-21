import os
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
DEBUG=os.getenv('APP_DEBUG')=='1'
SECRET_KEY=os.getenv('APP_SECRET') or ('local-development-only-not-for-production-42' if DEBUG else '')
if not SECRET_KEY:raise RuntimeError('APP_SECRET is required')
ALLOWED_HOSTS=os.getenv('APP_HOSTS','localhost,127.0.0.1,testserver').split(',')
CSRF_TRUSTED_ORIGINS=os.getenv('APP_ORIGINS','http://127.0.0.1:8106').split(',')
INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','portal']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','portal.middleware.PasswordChangeMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware','portal.middleware.HeadersMiddleware']
ROOT_URLCONF='portal.urls'
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','portal.views.common']}}]
WSGI_APPLICATION='portal.wsgi.application'
DATA_DIR=Path(os.getenv('APP_DATA',str(BASE_DIR/'data')))
DATA_DIR.mkdir(parents=True,exist_ok=True)
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':DATA_DIR/'portal.sqlite3','OPTIONS':{'timeout':30}}}
LANGUAGE_CODE='ru-ru'
TIME_ZONE='Europe/Moscow'
USE_I18N=True
USE_TZ=True
STATIC_URL='/normcontol/static/'
STATICFILES_DIRS=[BASE_DIR/'static']
STATIC_ROOT=BASE_DIR/'static-collected'
STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'portal.static_storage.PublicStaticFilesStorage'}}
MEDIA_ROOT=DATA_DIR/'documents'
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
LOGIN_URL='login'
LOGIN_REDIRECT_URL='dashboard'
LOGOUT_REDIRECT_URL='login'
SESSION_COOKIE_NAME='normcontrol_session'
CSRF_COOKIE_NAME='normcontrol_csrf'
SESSION_COOKIE_PATH='/normcontol/'
CSRF_COOKIE_PATH='/normcontol/'
AUTH_PASSWORD_VALIDATORS=[{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':12}},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'}]
SESSION_COOKIE_AGE=28800
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
SESSION_COOKIE_SECURE=not DEBUG
CSRF_COOKIE_SECURE=not DEBUG
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')
SECURE_SSL_REDIRECT=not DEBUG
SECURE_HSTS_SECONDS=31536000 if not DEBUG else 0
SECURE_CONTENT_TYPE_NOSNIFF=True
X_FRAME_OPTIONS='DENY'
FILE_UPLOAD_MAX_MEMORY_SIZE=1048576
DATA_UPLOAD_MAX_MEMORY_SIZE=120*1024*1024
DATA_UPLOAD_MAX_NUMBER_FILES=20
FILE_UPLOAD_PERMISSIONS=0o600
SITE_NAME=os.getenv('APP_NAME','Нормоконтроль')
LLM_EXECUTION_ENABLED=False
