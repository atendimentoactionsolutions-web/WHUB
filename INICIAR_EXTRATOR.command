#!/bin/zsh
cd "$(dirname "$0")"
export PATH="$(pwd)/.runtime/node/bin:$(pwd)/.runtime/python/bin:$PATH"

echo "================================================================"
echo "🚀 Iniciando Extrator Inteligente WhatsApp -> iCloud..."
echo "🌐 Abrindo painel no seu navegador: http://127.0.0.1:5050"
echo "================================================================"

# Abre o navegador automaticamente após 2 segundos
(sleep 2 && open "http://127.0.0.1:5050") &

./.runtime/python/bin/python3 app.py
