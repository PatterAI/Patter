FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JUPYTER_PLATFORM_DIRS=1 \
    PATTER_NOTEBOOKS_IN_DOCKER=1

WORKDIR /notebooks/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Pinned SDK + notebook helpers. PATTER_VERSION matches the default in _setup.py;
# override at build time with: docker build --build-arg PATTER_VERSION=0.5.4 .
ARG PATTER_VERSION=0.5.4
RUN pip install \
    "getpatter==${PATTER_VERSION}" \
    "jupyterlab>=4.2" \
    "ipykernel>=6.29" \
    "ipython>=8.0" \
    "nbclient>=0.10" \
    "jupyter-client>=8.0" \
    "python-dotenv>=1.0" \
    "httpx>=0.27" \
    "twilio>=9.0" \
    "telnyx>=2.0" \
    "pyngrok>=7.0"

# 8888 → JupyterLab. 8765 → EmbeddedServer for T2/T4 live cells.
EXPOSE 8888 8765

CMD ["jupyter", "lab", \
     "--ip=0.0.0.0", \
     "--port=8888", \
     "--no-browser", \
     "--allow-root", \
     "--ServerApp.token=", \
     "--ServerApp.password=", \
     "--ServerApp.root_dir=/notebooks/python"]
