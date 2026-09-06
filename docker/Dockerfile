# Single-service build (B.12): no worker target - the gateway is a
# synchronous request path, not a fan-out worker system.
# Build context is the parent directory of both `aikit` and
# `InferenceGateway` so this image can COPY the sibling aikit source (see
# docker-compose.yml's context: ../..).
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# git: pip needs it to clone the aikit@vX.Y.Z direct git+URL dependency.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY aikit /aikit
COPY InferenceGateway/pyproject.toml InferenceGateway/README.md InferenceGateway/alembic.ini ./
COPY InferenceGateway/src ./src

# CPU-only torch first: without this, pip resolves sentence-transformers'
# torch>=2.2 dependency to the default CUDA-enabled wheel, which drags in
# several GB of NVIDIA driver packages this CPU-only container never uses.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# Order matters: this project's own deps first (its pinned
# "aikit[gateway] @ git+...@vX" dep fetches aikit from that tag), then the
# locally COPY'd /aikit LAST so the image runs today's sibling source, not
# the tag - pip always re-resolves a direct-URL requirement rather than
# treating an already-installed package as satisfying it.
RUN pip install . && pip install "/aikit[gateway]"

EXPOSE 8000
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
