# Embedding-dienst voor ocd-api (OLLAMA_URL), voor Coolify op Hetzner.
#
# Op Railway stond deze dienst er zonder repo (geplaatst met `railway up`); dit is
# de vervanging. Het model MOET gelijk blijven aan het indexmodel van
# v2a.tekst_embedding (nomic-embed-text, 768 dimensies, op Railway image-id
# 0a109f422b47, Ollama 0.31.2 op 2026-10-27), anders passen de zoekvectoren niet bij de index.
# Zet in Coolify een volume op /root/.ollama, dan wordt het model maar één keer
# gedownload.
FROM ollama/ollama:0.31.2

ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434

# Server starten, het model ophalen als het er nog niet is, en de server
# op de voorgrond houden.
ENTRYPOINT ["/bin/sh", "-c", "ollama serve & until ollama list >/dev/null 2>&1; do sleep 1; done; ollama pull nomic-embed-text && wait"]
