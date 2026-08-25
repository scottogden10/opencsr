# OpenCSR — hosted demo image (mock backend: no API key, deterministic, free)
#
#   docker build -t opencsr .
#   docker run -p 8734:8734 opencsr
#
# Works as-is on Render, Railway, Fly.io, and Hugging Face Docker Spaces
# (for HF, set `app_port: 8734` in the Space README front matter).
# The demo story is re-seeded on every container start, so ephemeral
# filesystems give every restart a fresh, coherent ledger.
#
# Live mode in a container: add -e ANTHROPIC_API_KEY=... and change the CMD
# backend to `--backend managed` — only do this behind auth, never public.

FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 \
    OPENCSR_ALLOWED_HOSTS=*
EXPOSE 8734
# OPENCSR_BACKEND=managed (+ ANTHROPIC_API_KEY, OPENCSR_ACCESS_CODE,
# OPENCSR_SPEND_LIMIT_USD) turns the same image into a shared LIVE demo.
CMD ["sh", "-c", "python run_demo.py --force && python serve.py --host 0.0.0.0 --port ${PORT:-8734}"]
