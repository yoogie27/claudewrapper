FROM python:3.12-slim

# System dependencies: build tools, git, ssh, curl, node prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ca-certificates \
    gnupg \
    ripgrep \
    fzf \
    tree-sitter-cli \
    fd-find \
    bat \
    jq \
    yq \
    build-essential \
    gh \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# .NET 8 LTS
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet

# Go (latest stable)
RUN curl -fsSL https://go.dev/dl/go1.24.1.linux-amd64.tar.gz | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust (installed to shared location so claude user can access it)
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/usr/local/cargo/bin:${PATH}"

# CLI backends
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli @openai/codex

# Application
WORKDIR /app
COPY pyproject.toml .
COPY app/ app/
RUN pip install --no-cache-dir -e . scipy psutil pytest

# Create non-root user (required for Claude Code --dangerously-skip-permissions flag)
RUN useradd -m -u 1000 claude \
    && chown -R claude:claude $(npm root -g) $(dirname $(which npm)) 2>/dev/null || true

# Ensure ~/.local/bin is in PATH (Claude Code install script installs there)
ENV PATH="/home/claude/.local/bin:${PATH}"

# Git config (needed for commits inside the container)
RUN git config --global user.name "ClaudeWrapper" \
    && git config --global user.email "claudewrapper@docker"

# Entrypoint handles SSH setup and Claude Code updates, then drops to non-root user
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure app and data directories are owned by the non-root user
RUN chown -R claude:claude /app /data 2>/dev/null || true

EXPOSE 8645

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
