# OmniVoice Vite React UI

## Dev

Start backend from repo root:

```cmd
scripts\start_backend.cmd
```

Start frontend:

```cmd
scripts\start_frontend.cmd
```

Open:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`.

## Features

- Short TTS generation with clone/design/auto modes.
- Long text job submission for 2-3 hour narration.
- Job progress polling from backend manifest.
- Smart pause settings for comma/sentence/paragraph pauses.
- Cancel, log, and download actions.
