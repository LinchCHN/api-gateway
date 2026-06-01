# ⚡ API Gateway

A lightweight API gateway for managing multiple LLM providers (OpenAI, Claude, GLM, etc.) with a web GUI. Designed for ARM devices like OpenWrt routers.

**Features:**
- 🔄 Multi-provider support (OpenAI, Anthropic, and compatible APIs)
- 🔑 Gateway API keys (`sk-xxx`) — one key for all your providers
- 🎯 Model-level routing and access control
- 🔍 Auto-scan upstream models
- 📊 Request statistics
- 🐳 One-click Docker deployment
- 💻 Works on ARMv7 (32-bit) devices

## Quick Deploy

```bash
git clone https://github.com/LinchDesigner/api-gateway.git
cd api-gateway
docker compose up -d
```

Open `http://<device-ip>:3000` to access the dashboard.

## Usage

### 1. Add Providers
Add your API providers (OpenAI, Anthropic, or compatible services like GLM, MiMo, etc.)

| Field | Example |
|-------|---------|
| Base URL | `https://api.openai.com/v1` or `https://open.bigmodel.cn/api/coding/paas/v4` |
| API Key | Your provider's real API key |
| Service Type | `openai` or `anthropic` |

### 2. Scan & Route Models
Click **Scan** to auto-detect available models, then select which ones to route through this provider.

### 3. Generate Gateway Keys
Create `sk-xxx` keys and pick which models each key can access. Keys from different providers can be mixed.

### 4. Configure Your Tools

**Claude Code / Anthropic:**
```bash
export ANTHROPIC_BASE_URL=http://<gateway-ip>:3000
export ANTHROPIC_API_KEY=sk-your-gateway-key
```

**Codex / Cursor / OpenAI compatible:**
```bash
export OPENAI_BASE_URL=http://<gateway-ip>:3000/v1
export OPENAI_API_KEY=sk-your-gateway-key
```

**curl test:**
```bash
curl http://<gateway-ip>:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

## URL Handling

The gateway automatically handles URL path construction:
- `https://api.openai.com/v1` → no double `/v1`
- `https://open.bigmodel.cn/api/coding/paas/v4` → preserves `/v4`
- Anthropic providers → no `/v1` prefix added

## Architecture

```
Your Tools (Codex, Claude Code, etc.)
  → Gateway (sk-xxx key auth)
    → Model routing
      → Provider A (real key swapped in)
      → Provider B (real key swapped in)
      → Provider C (real key swapped in)
```

## Requirements

- Docker (any architecture: ARMv7, ARM64, x86_64)
- ~50MB RAM
- ~100MB disk

## License

MIT
