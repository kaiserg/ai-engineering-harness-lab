# Compilar Jenkins para Linux, no copiar el binario de macOS.
FROM golang:1.27.3-alpine AS jenkins
WORKDIR /src
COPY services/jenkins-mcp/go.mod services/jenkins-mcp/go.sum ./
RUN go mod download
COPY services/jenkins-mcp/ ./
RUN CGO_ENABLED=0 go build -trimpath -mod=readonly -o /out/jenkins-mcp .

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.18
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable
COPY harness/ ./harness/
COPY frontend/ ./frontend/
COPY --from=jenkins /out/jenkins-mcp ./bin/jenkins-mcp
RUN mkdir -p /app/data/runtime && chown 10001:10001 /app/data/runtime
# Bootstrap lee el secreto montado, prepara su copia privada y abandona root antes del servicio.
ENTRYPOINT ["python", "-m", "harness.container_entrypoint"]
CMD ["python", "-m", "harness.web", "--storage", "postgres", "--host", "0.0.0.0"]
