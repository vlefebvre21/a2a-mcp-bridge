# Deploying a Remote A2A Bridge Client on macOS with Hermes Agent

## Architecture

```
┌─────────────────────────┐         HTTP                ┌──────────────────────────┐
│       VPS (Cloud)       │◄───────────────────────────►│     Mac (Local/NAT)      │
│                         │                             │                          │
│  a2a-mcp-bridge         │   ┌──────────────────────┐  │  Ollama (local LLM)      │
│    serve-facade :8080   │   │  Reverse SSH tunnel  │  │  Hermes Agent            │
│  SQLite bus (master)    │   │  Mac → VPS           │  │  a2a-mcp-bridge serve    │
│  Wake registry          │   │  :8660 (webhook)     │  │    (remote mode)         │
│  N agents (Hermes)      │   └──────────────────────┘  │  Gateway webhook :8660   │
└─────────────────────────┘                             └──────────────────────────┘
```

The VPS hosts the master SQLite bus and the HTTP facade. The Mac connects as a remote client via the facade URL. The wake webhook is routed through a reverse SSH tunnel since the Mac sits behind residential NAT.

## Prerequisites

- **VPS**: `a2a-mcp-bridge` v0.6.0+ with `serve-facade` installed, port 8080 open (`ufw allow 8080/tcp`).
- **Mac**: macOS with Homebrew, Ollama with a loaded model, Python 3.11+.

## Step 1 — Install Hermes on the Mac

```bash
pip install hermes-agent
# or: pipx install hermes-agent
# or: uv tool install hermes-agent
```

Verify: `hermes --version`

## Step 2 — Install the A2A bridge

```bash
uv tool install "a2a-mcp-bridge[remote] @ git+https://github.com/vlefebvre21/a2a-mcp-bridge"
```

Verify: `a2a-mcp-bridge --help` should list `serve`, `serve-facade`, `init`, etc.

## Step 3 — Configure the facade on the VPS

Generate an API key and start the facade:

```bash
export A2A_FACADE_API_KEY="$(openssl rand -hex 32)"
echo "$A2A_FACADE_API_KEY" > ~/.a2a-facade-key
chmod 600 ~/.a2a-facade-key

a2a-mcp-bridge serve-facade \
  --host 0.0.0.0 \
  --port 8080 \
  --api-key "$A2A_FACADE_API_KEY"
```

Verify from the Mac:

```bash
curl -s http://<VPS_IP>:8080/health
# Expected: {"status":"ok","version":"0.6.0","agents":N}
```

## Step 4 — Test bridge connectivity (standalone)

On the Mac, run the bridge in remote mode:

```bash
A2A_AGENT_ID=my-mac-agent \
A2A_BUS_URL=http://<VPS_IP>:8080 \
A2A_FACADE_API_KEY=<facade_key> \
a2a-mcp-bridge serve
```

The log should show `registered successfully (HTTP 200 OK)`. Stop with `Ctrl+C`.

## Step 5 — Configure Hermes

Run `hermes config edit` and add the following sections.

### MCP server (bridge in remote mode)

```yaml
mcp_servers:
  a2a:
    command: a2a-mcp-bridge
    args:
      - serve
    env:
      A2A_AGENT_ID: my-mac-agent
      A2A_BUS_URL: http://<VPS_IP>:8080
      A2A_FACADE_API_KEY: <facade_key>
```

### Local model (Ollama)

```yaml
model:
  provider: openai
  model: <ollama_model_tag>
  base_url: http://127.0.0.1:11434/v1
```

### Webhook platform

Must be at the YAML root level, not nested under `display`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8660
      rate_limit: 10
      secret: <wake_hmac_secret>
```

> **Common pitfall**: Hermes config has both a `display.platforms` key (UI settings) and a root-level `platforms` key (messaging platforms). The webhook must be under the root-level one. Verify with `grep -n "^platforms:" ~/.hermes/config.yaml`.

### Allow open access

```bash
echo "GATEWAY_ALLOW_ALL_USERS=true" >> ~/.hermes/.env
```

## Step 6 — Create the webhook subscription

```bash
hermes webhook subscribe a2a-wake \
  --prompt "A2A bus message received. Read your inbox with agent_inbox and process messages." \
  --description "Wake on A2A bus message" \
  --skills "a2a-inbox-triage" \
  --deliver log \
  --secret "<wake_hmac_secret>"
```

> **Note**: Hermes expects the HMAC signature in the `X-Webhook-Signature` header without a `sha256=` prefix.

## Step 7 — Start the Hermes gateway

```bash
hermes gateway start
```

Verify the webhook port is listening:

```bash
lsof -i :8660
```

## Step 8 — Reverse SSH tunnel (Mac behind NAT)

The VPS must reach the Mac's webhook on port 8660. Install autossh and create a persistent tunnel:

```bash
brew install autossh

autossh -M 0 -f -N \
  -R 8660:127.0.0.1:8660 \
  user@<VPS_IP> -p <SSH_PORT> \
  -o "ServerAliveInterval=30" \
  -o "ServerAliveCountMax=3"
```

> **Common pitfall**: if the VPS uses a non-standard SSH port (e.g. 2222), you must specify `-p <SSH_PORT>`. Without it, autossh connects to port 22 and fails silently.

Verify from the VPS:

```bash
ss -tlnp | grep 8660
# Expected: LISTEN on 127.0.0.1:8660

curl -s -X POST http://127.0.0.1:8660/webhooks/a2a-wake
# Expected: 401 (missing signature — but the connection works)
```

## Step 9 — Register the agent in the wake registry

On the VPS, add the Mac agent to the wake registry:

```python
python3 -c "
import json
with open('$HOME/.a2a-wake-registry.json','r') as f:
    d=json.load(f)
d['agents']['my-mac-agent']={'wake_webhook_url':'http://127.0.0.1:8660/webhooks/a2a-wake'}
with open('$HOME/.a2a-wake-registry.json','w') as f:
    json.dump(d,f,indent=2)
print('Done -', len(d.get('agents',{})), 'agents')
"
```

## Step 10 — End-to-end test

From the VPS, send a manual wake:

```bash
SECRET="<wake_hmac_secret>"
BODY='{"agent_id":"test-sender","message_id":"test-e2e"}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8660/webhooks/a2a-wake \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$BODY"
```

Expected response: `{"status":"accepted",...}`. Verify on the Mac:

```bash
tail -20 ~/.hermes/logs/agent.log | grep "a2a-wake\|inbox\|agent_send"
```

## Port summary

| Service | Port | Location |
|---|---|---|
| A2A facade (HTTP API) | 8080 | VPS, public |
| Hermes webhook | 8660 | Mac local, exposed to VPS via SSH tunnel |
| Ollama | 11434 | Mac local |
| SSH | 2222 | VPS, public |

## Known issues

### Wake does not trigger automatically

The facade must call the webhook on `agent_send` to the Mac agent. If wake is manual only, the facade does not yet consult the wake registry on message insertion. This is the "last mile" to wire up.

### SSH tunnel drops

autossh restarts it automatically. Verify with `ps aux | grep autossh`. For a more robust solution, consider Tailscale.

### `platforms` misplaced in config.yaml

The `platforms` section must be at the YAML root, not nested under `display`. Symptom: `WARNING gateway.run: No messaging platforms enabled`.
