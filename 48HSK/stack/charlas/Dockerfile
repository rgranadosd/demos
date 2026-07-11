# Contenedor "charlas": agente 48HSK + Weather MCP + demo AI Gateway.
# El MCP corre como servicio (APIM lo proxya en localhost:28080, mismo namespace
# de red que APIM/IS). El agente se ejecuta con `podman exec` (ver README).
FROM docker.io/library/python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV APP=/opt/charlas/48HSK
WORKDIR ${APP}

# Dependencias: CLI del agente (Semantic Kernel + OpenAI) + Weather MCP
RUN pip install --no-cache-dir \
      "semantic-kernel==1.37.0" \
      "openai>=1.59,<2" \
      "python-dotenv>=1,<2" \
      "requests>=2.32" \
      httpx \
      "mcp[cli]" \
      uvicorn \
      fastapi \
      pydantic

# Snapshot de 48HSK sin venvs/caches (ver build.sh / .dockerignore)
COPY payload/ ${APP}/

# 28080 = Weather MCP · 8000 = callback OAuth del agente · 8502 = Streamlit aigateway-demo
EXPOSE 28080 8000 8502

# Servicio por defecto: el backend del Weather MCP
WORKDIR ${APP}/MCP/WEATHER
ENTRYPOINT ["python", "-m", "uvicorn", "weather_mcp_openmeteo:asgi_app", "--host", "0.0.0.0", "--port", "28080"]
