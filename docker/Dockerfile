FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
RUN useradd --no-log-init --uid 1026 --gid 100 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin ipsw
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chmod=644 server.py notifications.py /app/
COPY web /app/web
USER 1026:100
EXPOSE 8080
CMD ["python", "server.py"]
