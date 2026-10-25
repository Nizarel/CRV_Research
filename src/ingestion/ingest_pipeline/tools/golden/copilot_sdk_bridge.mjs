import { createInterface } from 'node:readline';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const lines = input[Symbol.asyncIterator]();
const initial = await lines.next();
const config = JSON.parse(initial.value);
const manifest = JSON.parse(await readFile(join(dirname(config.sdk_path), '..', 'package.json'), 'utf8'));
if (manifest.name !== '@github/copilot-sdk' || manifest.version !== '1.0.26') {
  throw new Error('Unexpected Copilot SDK package or version');
}
const { CopilotClient, RuntimeConnection } = await import(pathToFileURL(config.sdk_path).href);
const working = await mkdtemp(join(tmpdir(), 'crv-copilot-sdk-'));
const output = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
  !/^(OTEL_|LANGSMITH_|LANGCHAIN_|COPILOT_OTEL_)/.test(name)));
env.COPILOT_GITHUB_TELEMETRY_DISABLED = 'true';
const client = new CopilotClient({
  connection: RuntimeConnection.forStdio({ path: config.cli_path }),
  workingDirectory: working,
  env,
  useLoggedInUser: true,
  logLevel: 'error',
});
const active = new Set();

async function compare(request) {
  let session;
  const usage = { provider: 'github-copilot-sdk', sdk_version: manifest.version,
    model: config.model, events: [], cost_status: 'unmeasured' };
  try {
    session = await client.createSession({
      availableTools: [],
      tools: [],
      mcpServers: {},
      enableConfigDiscovery: false,
      enableOnDemandInstructionDiscovery: false,
      enableFileHooks: false,
      enableHostGitOperations: false,
      enableSkills: false,
      enableSessionStore: false,
      enableSessionTelemetry: false,
      skipEmbeddingRetrieval: true,
      memory: { enabled: false },
      infiniteSessions: { enabled: false },
      systemMessage: { mode: 'append', content: config.instructions },
      onPermissionRequest: async () => { throw new Error('Evaluation tools are prohibited'); },
    });
    await session.setModel(config.model);
    let timer;
    let content;
    const response = new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('SDK response timeout')), config.timeout_ms);
      session.on((event) => {
        if (event.agentId) return;
        if (event.type === 'assistant.usage') {
          usage.events.push({ type: event.type, data: Object.fromEntries(
            ['model', 'inputTokens', 'outputTokens', 'cacheReadTokens', 'cost', 'duration']
              .map((name) => [name, event.data[name] ?? null])) });
        }
        if (event.type === 'assistant.message' && !event.data.toolRequests?.length) {
          content = event.data.content;
        }
        if ((event.type === 'session.idle' || event.type === 'assistant.turn_end') && content) {
          resolve(content);
        }
        if (event.type === 'session.error') reject(new Error('SDK session error'));
      });
    });
    try {
      await session.send({ prompt: `Return ONLY JSON matching this schema:\n${JSON.stringify(request.schema)}\nEvaluation data:\n${JSON.stringify(request.payload)}`,
        responseSchema: request.schema });
      const result = await response;
      output({ id: request.id, status: 'ok', content: result, usage });
    } finally {
      clearTimeout(timer);
    }
  } catch (error) {
    if (session) await session.abort().catch(() => {});
    output({ id: request.id, status: 'error', error_type: error.name, usage });
  } finally {
    if (session) await client.deleteSession(session.sessionId).catch(() => {});
  }
}

try {
  await client.start();
  const auth = await client.getAuthStatus();
  if (!auth.isAuthenticated) throw new Error('Copilot authentication unavailable');
  const models = await client.listModels();
  if (!models.some((model) => model.id === config.model)) throw new Error('Copilot model unavailable');
  const status = await client.getStatus();
  output({ event: 'ready', model: config.model, sdk_version: manifest.version,
    runtime_version: status.version });
  for await (const line of { [Symbol.asyncIterator]: () => lines }) {
    const request = JSON.parse(line);
    if (request.operation === 'stop') break;
    const task = compare(request);
    active.add(task);
    task.finally(() => active.delete(task));
  }
  await Promise.all(active);
} catch (error) {
  output({ event: 'fatal', error_type: error.name });
  process.exitCode = 1;
} finally {
  input.close();
  process.stdin.destroy();
  const errors = await client.stop().catch(() => [new Error('SDK shutdown failed')]);
  if (errors.length) await client.forceStop();
  await rm(working, { recursive: true, force: true });
  await new Promise((resolve) => process.stdout.write('', resolve));
  process.exit(process.exitCode ?? 0);
}