"""Dockerfile templates (Node, Python, Vite/static nginx) with clear placeholders."""
from __future__ import annotations

from string import Template


NODE_TEMPLATE = Template(
    """\
# ---------- Node.js application ($FRAMEWORK_NOTE) ----------
FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install --production

COPY . .

EXPOSE $PORT

CMD $START_COMMAND
"""
)


PYTHON_TEMPLATE = Template(
    """\
# ---------- Python application ($FRAMEWORK_NOTE) ----------
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE $PORT

CMD $START_COMMAND
"""
)


# Shown to the LLM with $PLACEHOLDER names; dollars for Docker ARG/ENV are
# written as ${...} style in guidance — the filled output must be valid Docker.
STATIC_TEMPLATE_SKELETON = """\
# ---------- SPA / static frontend ($FRAMEWORK_NOTE) ----------
# Stage 1: npm build (Vite / React / similar)
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
ARG VITE_API_BASE_URL=
ARG VITE_SUPABASE_URL=
ARG VITE_SUPABASE_ANON_KEY=
ARG VITE_USE_MOCK=false
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL
ENV VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY
ENV VITE_USE_MOCK=$VITE_USE_MOCK
RUN npm run build

# Stage 2: nginx serves $STATIC_SOURCE (usually dist) on port 80
FROM nginx:alpine
COPY nginx.spa.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/$STATIC_SOURCE /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
"""


TEMPLATES: dict[str, Template] = {
    "node": NODE_TEMPLATE,
    "python": PYTHON_TEMPLATE,
}


def describe_templates() -> str:
    """Render each template skeleton for the prompt."""
    parts = [
        f"--- {name} template ---\n{tmpl.template}" for name, tmpl in TEMPLATES.items()
    ]
    parts.append(f"--- static template ---\n{STATIC_TEMPLATE_SKELETON}")
    return "\n\n".join(parts)


_NGINX_SPA_CONF = """\
server {
  listen 80;
  server_name _;
  root /usr/share/nginx/html;
  index index.html;

  location / {
    try_files $uri $uri/ /index.html;
  }
}
"""


def render_static_dockerfile(
    *,
    framework_note: str = "Vite/React SPA",
    static_source: str = "dist",
) -> str:
    """Deterministic multi-stage Dockerfile for SPA frontends."""
    return f"""\
# ---------- SPA / static frontend ({framework_note}) ----------
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
ARG VITE_API_BASE_URL=
ARG VITE_SUPABASE_URL=
ARG VITE_SUPABASE_ANON_KEY=
ARG VITE_USE_MOCK=false
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL
ENV VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY
ENV VITE_USE_MOCK=$VITE_USE_MOCK
RUN npm run build

FROM nginx:alpine
COPY nginx.spa.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/{static_source} /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
"""


def ensure_nginx_spa_conf(repo_path: str) -> None:
    """Write nginx.spa.conf into the build context (required by static Dockerfile)."""
    import os

    path = os.path.join(repo_path, "nginx.spa.conf")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_NGINX_SPA_CONF)
