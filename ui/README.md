# DrawDiff UI

React + Tailwind SPA for the DrawDiff comparison pipeline.

## Dev

```bash
cd ui
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:5173` and proxies `/compare` and
`/health` to the FastAPI backend on `http://localhost:8000` — start that first:

```bash
uvicorn api.main:app --reload
```

## Build

```bash
npm run build
npm run preview
```

## Architecture

- **[src/api.ts](src/api.ts)** — typed client for `POST /compare` and
  `GET /compare/{job_id}`. Matches the actual backend contract
  (`status: queued|processing|complete|failed`, `progress_pct`, presigned
  `report_url` / `changeset_url` delivered inline with the status payload).
- **[src/App.tsx](src/App.tsx)** — three-view state machine: `upload →
  progress → results`.
- **[src/components/UploadView.tsx](src/components/UploadView.tsx)** —
  drag-and-drop dual PDF upload + metadata, submits via `useMutation`.
- **[src/components/ProgressView.tsx](src/components/ProgressView.tsx)** —
  `useQuery` with a 2s `refetchInterval` that stops on `complete` / `failed`
  so we don't keep presigning S3 URLs after the job is done.
- **[src/components/ResultsView.tsx](src/components/ResultsView.tsx)** —
  severity badge row (Critical / Significant / Minor / Uncertain) + download
  buttons.
