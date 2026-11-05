# WHUB — Sincronizador Inteligente de Contatos (`whub`)

Sistema isolado e dedicado em **Python + Node.js (Baileys v7)** para sincronizar, filtrar e organizar contatos novos das conversas individuais (1-a-1) do WhatsApp para o **Apple iCloud** (`.vcf` vCard 3.0) sem duplicar nem apagar nenhum contato existente.

## Recursos Principais
- **Mapeamento Completo (`@lid` + `@s.whatsapp.net`)**: Compatível com WhatsApp Business e contas com protocolo LID da Meta.
- **Tripla Proteção Anti-Duplicatas**:
  1. Detecta contatos já salvos na agenda do celular sincronizada com o WhatsApp.
  2. Permite enviar um arquivo `.vcf` atual do iCloud para cruzamento de números (com inteligência para 8 e 9 dígitos no Brasil).
  3. Registra memória de exportação (`exported_history.json`) para não repetir contatos já exportados anteriormente.
- **Personalização de Nomenclatura**:
  - `Cliente 101`, `Cliente 102`... (pulando automaticamente números que já existem na sua agenda).
  - `Cliente Mundo Apple 101`...
  - Prefixo e número inicial 100% customizáveis + opção de incluir o nome do perfil do WhatsApp.

## Como Rodar na VPS (Isolado via Docker)

```bash
git clone <URL_DO_REPOSITORIO> contato-extrator
cd contato-extrator
docker compose up -d --build
```

O sistema ficará disponível na porta `5050` (ou na porta definida na variável `PORT`), totalmente isolado em seu próprio container `contato-extrator-whatsapp-icloud`.
