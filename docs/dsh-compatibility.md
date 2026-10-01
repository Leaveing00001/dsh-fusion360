# DSH compatibility notes

What this project depends on in DeepSeek Harness, and what was re-checked when
DSH Desktop moved **2.0.9 → 2.0.17**.

Everything below was read out of the installed DSH on 2026-10-01, not recalled.

| Component | Version |
| --- | --- |
| DSH Desktop | 2.0.17 |
| `@deepseek-ai/dsh` | 0.2.0-rc.2 |
| `@deepseek-ai/dsh-mcp-client` | 0.2.0-rc.2 |
| `@modelcontextprotocol/client` | 2.0.0 |
| `@modelcontextprotocol/core` | 2.0.0 |

**Result: nothing broke.** The only changes required were a stale comment in the
MCP server, a manifest version that had been wrong since 1.0.0, and the new
`instructions` field documented below.

## Where DSH keeps the things this project touches

| What | Where |
| --- | --- |
| The plugin config this project edits | `~/.dsh/profiles/<profile>/cordis.patch.yml` |
| The mcp-client implementation | `<install>/resources/app/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js` |
| The env scrub shared by all spawners | `<install>/resources/app/node_modules/@deepseek-ai/dsh-subprocess/lib/index.js` |
| Protocol constants | `<install>/resources/app/node_modules/@modelcontextprotocol/core/dist/auth-*.mjs` |

Earlier DSH builds shipped `<install>/resources/app.asar`; **2.0.17 ships the
same tree unpacked** at `<install>/resources/app/`. Reading it no longer needs an
asar extractor — plain file reads work. (`tools/asar.ps1` is kept because older
installs still need it.)

## The checks, and what they say

### 1. `mcp-client` config schema — unchanged for our keys

The stdio branch of the config union in `dsh-mcp-client` accepts:

```
transport: 'stdio'          serverName: string   command: string
args: string[]              env: {string}        cwd: string
toolCallTimeoutMs: number   failOnStartupError: boolean
maxInstructionBytes: number reconnect: { enabled, initialDelayMs, maxDelayMs, maxAttempts }
```

We use `transport`, `serverName`, `command`, `args`, `cwd`, `env`,
`toolCallTimeoutMs`, `failOnStartupError`. All still present with the same
meaning.

Two options are **new** and unused here:

- `reconnect` — an exponential-backoff reconnect policy. Relevant to us only if
  the MCP server process dies; it does not help when *Fusion* is simply closed,
  because the server starts fine and reports the bridge error itself.
- `maxInstructionBytes` — caps the `instructions` string (default 2048). Our
  `SERVER_INSTRUCTIONS` is well under it.

### 2. Tool naming — unchanged

Still `mcp__<serverName>__<rawName>`, verified in
`dsh-mcp-client/lib/index.js`. Nothing else in the name is parsed to recover the
raw name. Our 13 tools still appear as `mcp__fusion360__*`.

`serverName` must match `/^[A-Za-z0-9_-]{1,32}$/` and is reserved
**uniquely per registration scope** — two `mcp-client` instances with the same
`serverName` throw. `fusion360` is 9 characters. Fine.

### 3. MCP protocol versions — exact match, so no change

`@modelcontextprotocol/core` 2.0.0 declares:

```js
LATEST_PROTOCOL_VERSION = '2025-11-25'
DEFAULT_NEGOTIATED_PROTOCOL_VERSION = '2025-03-26'
SUPPORTED_PROTOCOL_VERSIONS = ['2025-11-25','2025-06-18','2025-03-26','2024-11-05','2024-10-07']
```

`mcp_server/fusion_mcp_server.py` lists the same five versions, in the same
order, and its `FALLBACK_PROTOCOL_VERSION` is deliberately equal to
`DEFAULT_NEGOTIATED_PROTOCOL_VERSION`. Whichever version DSH asks for is echoed
back unchanged.

### 4. The env scrub — why `FUSION_DSH_BRIDGE_*` survives

`dsh-subprocess` scrubs the environment it hands to every child:

```js
const SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i
// keep a name only if it is not credential-shaped and does not start with DSH_
```

`mcp-client` builds the child env as `{ ...scrubbedParentEnv(), ...extra }` — the
spec's explicit `env:` is spread **after** the scrub.

So both facts matter, and both are in our favour:

- `FUSION_DSH_BRIDGE_HOST` / `FUSION_DSH_BRIDGE_PORT` do not start with `DSH_`
  (the prefix test is `startsWith`, so the embedded `DSH_` is irrelevant), and
  they contain none of `KEY`/`PASSWORD`/`SECRET`/`TOKEN`.
- Even if they had, they are passed in the explicit `env:` block, which is never
  scrubbed.

**The trap to avoid:** a name that *is* credential-shaped and is expected to be
*inherited* rather than declared will silently disappear. Declare bridge settings
in `env:`; do not rely on inheriting them from the DSH process.

### 5. Timeouts

`DEFAULT_TOOL_CALL_TIMEOUT_MS` is 60000. The profile config sets
`toolCallTimeoutMs: 120000`, which is deliberate: a cold first call has to wait
for the add-in's event round-trip, and Fusion's own cold start is ~85 s before
port 27182 listens.

### 6. `instructions` — new, and now used

`mcp-client` appends a server's `instructions` to the agent's system prompt as
`### MCP server: <serverName>`, bounded by `maxInstructionBytes`. The server
previously sent none, so the agent saw 13 tools and no hint that a desktop
application had to be running.

`fusion_mcp_server.py` now returns `SERVER_INSTRUCTIONS` from `initialize`. It
states that Fusion must be running, that calls are serialised on Fusion's primary
thread, that a cold call takes ~25 s, what `fusion_run_python` pre-binds, and
that the API is in centimetres and radians.

### 7. MCP resources — available, not used

`@deepseek-ai/dsh-mcp-resources` is now a dependency of `dsh-mcp-client`, and
DSH exposes `list_mcp_resources` / `read_mcp_resource`. Our server advertises no
resources and returns none, which is why those tools report an empty list for
`fusion360`. Nothing is broken; it is simply a capability we do not implement.

The older DSH build did not have this package at all.

### 8. Profile plugin ids still resolve

Every `id` targeted by the patch layer in the live profile
(`ui-settings-general`, `agent-default-model`,
`dsh-tool-subagent/model-selection-settings`, `ui-settings-account`, `ui-chat`,
`ui-settings`) still exists in 2.0.17, and
`@deepseek-ai/dsh-tool-subagent` still exports `./model-selection-settings`.

## Re-running this audit after a DSH update

The install path on Windows is
`%LOCALAPPDATA%\Programs\...` or a custom directory; find it from a running
instance rather than guessing:

```powershell
Get-Process -Name 'DSH Desktop' | Select-Object -ExpandProperty Path
```

Then:

```powershell
$app = '<install>\resources\app'

# 1. versions
(Get-Item '<install>\DSH Desktop.exe').VersionInfo.FileVersion
(Get-Content "$app\node_modules\@deepseek-ai\dsh-mcp-client\package.json" -Raw | ConvertFrom-Json).version

# 2. config schema: find the z.object union and compare the keys
Select-String -Path "$app\node_modules\@deepseek-ai\dsh-mcp-client\lib\index.js" `
  -Pattern 'transport: z.const|toolCallTimeoutMs|failOnStartupError|serverName|reconnect'

# 3. the env scrub has not changed shape
Select-String -Path "$app\node_modules\@deepseek-ai\dsh-subprocess\lib\index.js" `
  -Pattern 'SENSITIVE_ENV_PATTERN|DSH_ENV_PREFIX|startsWith'

# 4. protocol constants: these must still match SUPPORTED_PROTOCOL_VERSIONS
#    in mcp_server/fusion_mcp_server.py
Select-String -Path "$app\node_modules\@modelcontextprotocol\core\dist\*.mjs" `
  -Pattern 'LATEST_PROTOCOL_VERSION =|DEFAULT_NEGOTIATED_PROTOCOL_VERSION ='

# 5. the tool-name contract
Select-String -Path "$app\node_modules\@deepseek-ai\dsh-mcp-client\lib\index.js" `
  -Pattern 'publicToolName|mcp__'
```

Then the live checks, which are the ones that actually matter:

```powershell
python tools\smoke_test.py                 # 16/16
python tools\addin_stub_test.py            # 22/22
python tools\verify_e2e.py                 # 12/12, needs Fusion running
```

and, from DSH itself, that `fusion_status` answers and
`examples/build_a_part.py` still reproduces 48000.00 / 47752.78 / 47243.84.

## What this audit does not cover

- **Only the versions in the table above.** Fusion removes and renames API
  members between releases; this says nothing about Fusion, only about the DSH
  side. See `fusion-api-notes.md` for that half.
- **A DSH release that changes the config schema in a breaking way.** The audit
  compares keys by eye against a hard-coded list. It is a checklist, not a test.
- **Whether the maintainers intend the current schema to stay stable.**
  `0.2.0-rc.2` is a release candidate; `reconnect` and `maxInstructionBytes`
  appearing between 2.0.9 and 2.0.17 shows the surface is still moving.
- **The GUI.** All of the above is the host/runtime side. Nothing here was
  checked against the Desktop renderer.
