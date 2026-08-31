"""Dockerfile templates (Node, Python, static/nginx) with clear placeholders.

The "template + LLM-fill" approach: instead of letting the model write Docker
syntax freeform (the source of broken builds), we give a well-formed skeleton
with `$PLACEHOLDER`s and ask it to only fill the blanks. This makes retries
tractable: a bad patch is a wrong *value*, not malformed *syntax*.

Note on `CMD`: we use SHELL form (`CMD node server.js`) rather than exec form
(`CMD ["node", "server.js"]`) for the runtime command. Shell form keeps a single
placeholder substitution valid without the model having to split a string into
an argv array — far less error-prone for LLM patch loops.
"""
from __future__ import annotations

from string import Template


# ---------------------------------------------------------------------------
# Node.js / Express
# ---------------------------------------------------------------------------
NODE_TEMPLATE = Template(
    """\
# ---------- Node.js application ($FRAMEWORK_NOTE) ----------
FROM node:20-alpine

# Set the working directory inside the container.
WORKDIR /app

# Install dependencies FIRST (layer-cache friendly: code changes don't
# re-trigger npm install as long as package*.json are unchanged).
COPY package*.json ./
RUN npm install --production

# Copy the rest of the application source.
COPY . .

# The port the app listens on (filled from the fingerprint).
EXPOSE $PORT

# Shell-form launch command, e.g.  node server.js
CMD $START_COMMAND
"""
)


# ---------------------------------------------------------------------------
# Python (Flask / FastAPI)
# ---------------------------------------------------------------------------
PYTHON_TEMPLATE = Template(
    """\
# ---------- Python application ($FRAMEWORK_NOTE) ----------
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies. Copy-then-install keeps layers cacheable.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# For pyproject.toml projects the DEPS layer would instead be:
#   COPY pyproject.toml ./
#   RUN pip install --no-cache-dir .

# Copy the rest of the source.
COPY . .

EXPOSE $PORT

# Launch the app, e.g.  uvicorn main:app --host 0.0.0.0 --port 8000
# or  flask --app app run --host 0.0.0.0 --port 5000
CMD $START_COMMAND
"""
)


# ---------------------------------------------------------------------------
# Static site / nginx
# ---------------------------------------------------------------------------
STATIC_TEMPLATE = Template(
    """\
# ---------- Static site served by nginx ($FRAMEWORK_NOTE) ----------
FROM nginx:alpine

# Copy the pre-built static files into nginx's web root.
# $STATIC_SOURCE is usually 'dist' (filled from the fingerprint) or '.' if the
# HTML is at the repo root.
COPY $STATIC_SOURCE /usr/share/nginx/html

# nginx listens on port 80 by default.
EXPOSE 80
"""
)


# Framework key (matching contracts.Framework.value) -> template.
TEMPLATES: dict[str, Template] = {
    "node": NODE_TEMPLATE,
    "python": PYTHON_TEMPLATE,
    "static": STATIC_TEMPLATE,
}


def describe_templates() -> str:
    """Render each template skeleton (placeholders intact) for the prompt.

    The model sees these skeletons and is told to pick+fill one. Keeping the
    placeholders visible (rather than pre-filled) guarantees it knows every
    knob it's allowed to touch.
    """
    return "\n\n".join(f"--- {name} template ---\n{tmpl.template}" for name, tmpl in TEMPLATES.items())
