FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY config ./config
# Container runtime does not need root privileges.
RUN groupadd --gid 10001 aiops && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aiops && \
    mkdir -p /app/data && chown -R aiops:aiops /app
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
