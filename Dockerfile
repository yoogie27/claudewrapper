FROM python:3.12-slim

# ── Layer 1: System packages (rarely changes) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ca-certificates \
    gnupg \
    ripgrep \
    fzf \
    fd-find \
    bat \
    jq \
    build-essential \
    gh \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: Language runtimes (rarely changes) ──

# Node.js 22 LTS
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# .NET 8 LTS
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet

# Go
RUN curl -fsSL https://go.dev/dl/go1.24.1.linux-amd64.tar.gz | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/usr/local/cargo/bin:${PATH}"

# ── Layer 3: CLI backends (changes when upgrading CLIs) ──
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli @openai/codex

# ── Layer 4: Python deps (changes when pyproject.toml changes) ──
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . scipy psutil pytest

# ── Layer 5: User setup (rarely changes) ──
RUN useradd -m -u 1000 claude \
    && chown -R claude:claude $(npm root -g) $(dirname $(which npm)) 2>/dev/null || true

ENV PATH="/home/claude/.local/bin:${PATH}"

RUN git config --global user.name "ClaudeWrapper" \
    && git config --global user.email "claudewrapper@docker"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Layer 6: App code (changes frequently — MUST be last) ──
COPY app/ app/
RUN chown -R claude:claude /app /data 2>/dev/null || true

EXPOSE 8645

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
