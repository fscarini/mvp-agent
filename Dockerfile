# ================================================================
# Dockerfile - Build Otimizado para Easypanel
# ================================================================

# ===== STAGE 1: Base - Define a imagem base e variáveis de ambiente =====
FROM python:3.11-slim as base

# Variáveis de ambiente padrão
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# ===== STAGE 2: Dependencies - Instala dependências (incluindo libs de compilação) =====
FROM base as dependencies

# Instala ferramentas de sistema para compilar dependências como pyaudio
RUN apt-get update && apt-get install -y --no-install-recommends \
    portaudio19-dev \
    gcc \
    python3-dev \
    libasound2-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Instala as dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# ===== STAGE 3: Application - Imagem final de produção (mais leve) =====
FROM base as application

# Instala libs de runtime necessárias (curl para healthcheck, libs de áudio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libasound2 \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Copia as dependências Python já instaladas e compiladas do estágio anterior
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=dependencies /usr/local/bin /usr/local/bin

# Cria usuário não-root (boa prática de segurança)
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/logs && \
    chown -R appuser:appuser /app

# Copia o código da aplicação (o app.py está na raiz)
COPY app.py .

# Define o usuário que vai rodar o container
USER appuser

# Expõe a porta
EXPOSE 5050

# Healthcheck que usa a rota de teste do seu FastAPI
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Comando de execução: Roda o FastAPI (objeto 'app' no arquivo 'app.py')
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5050"]