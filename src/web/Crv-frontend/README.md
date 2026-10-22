# CRV web

React + TypeScript + Vite front end for the CRV research agent (Reminex branding).

It talks to the backend in [src/backend](../../backend) through `/api/v1`:

- `POST /api/v1/conversations` creates a thread on the first question.
- `POST /api/v1/chat` streams `progress`, then `answer`, then `done` (SSE).
- `GET /api/v1/conversations/{id}` restores a conversation.
- `POST /api/v1/feedback` records a thumbs up/down against a run.

Answers cite sources as `[<evidence id>]`; the UI numbers them and opens the cited passage
with its exact quotations highlighted.

## Develop

Start the backend (`crv-agent serve`, with `CRV_LOCAL_OWNER` set for loopback development),
then:

```powershell
npm install
npm run dev      # proxies /api and /health to http://127.0.0.1:8000
npm test
npm run build
```

Set `VITE_API_BASE_URL` to use a separately hosted API instead of same-origin.

Entra bearer tokens are not attached yet: the UI works against the backend's local-owner mode.
