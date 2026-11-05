#!/usr/bin/env bash
# ==============================================================
# Script de Instalação e Deploy Automático na VPS (Ubuntu / Debian)
# WHUB — Extrator Inteligente de Contatos WhatsApp -> iCloud (Porta 5050)
# ==============================================================

set -e

echo "=========================================================="
echo "🚀 INICIANDO DEPLOY DO WHUB NA VPS"
echo "=========================================================="

# 1. Atualização do Sistema e Pacotes Básicos
echo "📦 [1/5] Atualizando pacotes do sistema..."
sudo apt-get update -y
sudo apt-get install -y curl wget git python3 python3-pip python3-venv build-essential ufw

# 2. Instalação do Node.js 20 LTS (se não instalado)
if ! command -v node &> /dev/null; then
    echo "📦 [2/5] Instalando Node.js 20 LTS..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    echo "✅ [2/5] Node.js já instalado ($(node -v))"
fi

# 3. Instalação das dependências do Node.js (Baileys, pino, qrcode)
echo "📦 [3/5] Instalando dependências npm..."
npm install --omit=dev

# 4. Instalação das dependências Python em ambiente virtual isolado (PEP 668 compliant)
echo "📦 [4/5] Configurando ambiente virtual Python e instalando dependências..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 5. Configuração do Serviço Systemd (para rodar 24/7 reiniciando automaticamente)
echo "⚙️ [5/5] Configurando serviço em segundo plano (systemd)..."

APP_DIR=$(pwd)
USER_NAME=$(whoami)

sudo tee /etc/systemd/system/whub.service > /dev/null <<EOF
[Unit]
Description=WHUB - Extrator Inteligente de Contatos WhatsApp para iCloud
After=network.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python3 ${APP_DIR}/app.py
Restart=always
RestartSec=5
Environment=PATH=${APP_DIR}/venv/bin:/usr/bin:/usr/local/bin:${PATH}
Environment=HOST=0.0.0.0
Environment=PORT=5050

[Install]
WantedBy=multi-user.target
EOF

# Libera a porta 5050 no firewall caso o UFW esteja ativo
sudo ufw allow 5050/tcp 2>/dev/null || true

# Ativa e reinicia o serviço
sudo systemctl daemon-reload
sudo systemctl enable whub
sudo systemctl restart whub

# Obtém IP público
PUB_IP=$(curl -s https://api.ipify.org || echo "IP_DA_SUA_VPS")

echo "=========================================================="
echo "✅ DEPLOY DO WHUB CONCLUÍDO COM SUCESSO!"
echo "🌐 Acesse no seu navegador: http://${PUB_IP}:5050"
echo "=========================================================="
echo "Comandos úteis no terminal da VPS:"
echo " • Ver status: sudo systemctl status whub"
echo " • Ver logs:   sudo journalctl -u whub -f"
echo " • Reiniciar:  sudo systemctl restart whub"
echo " • Parar:      sudo systemctl stop whub"
echo "=========================================================="
