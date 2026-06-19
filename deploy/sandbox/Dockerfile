# JARVIS Code-Sandbox — isolierter Ausführungs-Container (Tier 3).
# Führt vom Agenten geschriebenen Python-/Shell-Code aus + browser_control (headless Chromium),
# getrennt vom Host. Basis = offizielles Playwright-Image (Chromium + Systemdeps vorinstalliert).
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# util-linux liefert `unshare` (Netz-Isolation pro Job, wenn Internet deaktiviert ist)
RUN apt-get update && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Nicht-root-Nutzer = vorhandener pwuser des Basis-Images; Workspace als Volume
RUN mkdir -p /workspace && chown pwuser:pwuser /workspace
COPY sandbox_server.py /app/sandbox_server.py

USER pwuser
ENV HOME=/home/pwuser MPLCONFIGDIR=/tmp/mpl
WORKDIR /workspace
EXPOSE 8090
CMD ["uvicorn", "sandbox_server:app", "--host", "0.0.0.0", "--port", "8090", "--app-dir", "/app"]
