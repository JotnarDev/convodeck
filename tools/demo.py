#!/usr/bin/env python3
"""Build a fake Claude Code data folder for screenshots and tests (never touches your real data).

  python3 tools/demo.py /tmp/convodeck-demo [en|es]
  CLAUDE_CONFIG_DIR=/tmp/convodeck-demo/claude XDG_CONFIG_HOME=/tmp/convodeck-demo/config \\
  XDG_CACHE_HOME=/tmp/convodeck-demo/cache python3 skills/convodeck/scripts/convodeck.py ui
"""
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/convodeck-demo').resolve()
lang = sys.argv[2] if len(sys.argv) > 2 else 'en'
home = root / 'home'

SESSIONS = {
    'en': [
        ('', 0.1, 'Fix the flaky login test', 'Login test fixed by awaiting the session cookie; CI green on main again', 'the login e2e test fails one run out of five'),
        ('acme-shop', 2, 'Checkout redesign', 'Checkout split into three steps with Stripe payment element; mobile layout still pending', 'redesign the checkout page'),
        ('acme-shop', 26, 'Product search with Postgres', 'Full-text search with tsvector and trigram fallback, 40 ms p95 on 200k products', 'add search to the product list'),
        ('', 30, 'Dotfiles and shell setup', 'Zsh, starship and fzf configured; dotfiles repo created with an install script', 'set up my terminal nicely'),
        ('blog', 72, 'Migrate blog to Astro', 'Blog moved from Gatsby to Astro, images optimized, RSS and sitemap kept', 'move my blog from gatsby to astro'),
        ('api-gateway', 120, 'Rate limiting middleware', 'Token-bucket rate limiter with Redis, per-key limits and 429 headers, tests passing', 'add rate limiting to the API'),
        ('', 200, 'Back up the laptop', 'Restic backups to an external drive every night, restore tested on one folder', 'how do I back up this laptop'),
        ('mobile-app', 340, 'Push notifications', 'Expo push notifications wired to the backend; iOS certificate still needs renewing', 'add push notifications'),
    ],
    'es': [
        ('', 0.1, 'Arreglar el test de login', 'Test de login arreglado esperando la cookie de sesión; CI en verde otra vez', 'el test e2e de login falla una de cada cinco veces'),
        ('tienda-acme', 2, 'Rediseño del checkout', 'Checkout en tres pasos con el formulario de pago de Stripe; falta la vista móvil', 'rediseña la página de pago'),
        ('tienda-acme', 26, 'Búsqueda de productos en Postgres', 'Búsqueda de texto con tsvector y trigramas, 40 ms p95 con 200k productos', 'añade búsqueda al listado de productos'),
        ('', 30, 'Dotfiles y terminal', 'Zsh, starship y fzf configurados; repo de dotfiles con script de instalación', 'deja bonita mi terminal'),
        ('blog', 72, 'Migrar el blog a Astro', 'Blog pasado de Gatsby a Astro, imágenes optimizadas, RSS y sitemap intactos', 'pasa mi blog de gatsby a astro'),
        ('api-gateway', 120, 'Límite de peticiones', 'Limitador token-bucket con Redis, límites por clave y cabeceras 429, tests en verde', 'añade rate limiting a la API'),
        ('', 200, 'Copias de seguridad', 'Copias nocturnas con restic a un disco externo, restauración probada', 'cómo hago copias de este portátil'),
        ('app-movil', 340, 'Notificaciones push', 'Notificaciones push de Expo conectadas al backend; falta renovar el certificado de iOS', 'añade notificaciones push'),
    ],
}

data = {}
now = time.time()
for folder, hours_ago, title, summary, first in SESSIONS[lang]:
    cwd = home / 'Projects' / folder if folder else home
    cwd.mkdir(parents=True, exist_ok=True)
    # Claude Code names project folders after the path with every non-alphanumeric character as '-'
    proj = root / 'claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(cwd))
    proj.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'convodeck-demo/{lang}/{title}'))
    ts = datetime.fromtimestamp(now - hours_ago * 3600, timezone.utc).isoformat().replace('+00:00', 'Z')
    lines = [
        {'type': 'user', 'cwd': str(cwd), 'timestamp': ts, 'message': {'role': 'user', 'content': first}},
        {'type': 'assistant', 'timestamp': ts, 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': summary}]}},
    ]
    (proj / f'{sid}.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\n')
    data[sid] = {'title': title, 'summary': summary, 'n_summary': 1}

cfg = root / 'config' / 'convodeck'
cfg.mkdir(parents=True, exist_ok=True)
(cfg / 'data.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(root)
