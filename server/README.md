# OmniVoice FastAPI Backend

Run from repository root:

```bash
python -m uvicorn server.api:app --host 127.0.0.1 --port 8000 --reload
```

Environment variables:

- `OMNIVOICE_MODEL`: model id/path, default `k2-fsa/OmniVoice`
- `OMNIVOICE_DEVICE`: `cuda`, `cuda:0`, or `cpu`
- `OMNIVOICE_DTYPE`: `auto`, `float16`, `bfloat16`, `float32`
- `OMNIVOICE_CORS_ORIGINS`: comma-separated origins, default `*`

Endpoints:

- `GET /api/health`
- `POST /api/tts/short`
- `POST /api/jobs/long`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/jobs/{job_id}/download`
- `GET /api/jobs/{job_id}/log`
