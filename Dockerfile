FROM python:3.11-slim

# System dependencies: build tools, git, ssh, curl, node prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Application
WORKDIR /app
COPY pyproject.toml .
COPY app/ app/
RUN pip install --no-cache-dir -e .

# Entrypoint handles SSH setup and Claude Code updates
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Git config (needed for commits inside the container)
RUN git config --global user.name "ClaudeWrapper" \
    && git config --global user.email "claudewrapper@docker"

EXPOSE 8645

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
