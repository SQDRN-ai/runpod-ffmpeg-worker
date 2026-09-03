# Birthday workers for Hostinger

This independent Docker Compose stack hosts two CPU workloads currently handled
by RunPod:

- `render-*`: the 4K FFmpeg video and thumbnail renderer;
- `voice-*`: Demucs-based vocal removal / instrumental creation.

The karaoke aligner intentionally remains on RunPod because its current handler
requires CUDA (`WhisperX` + Demucs).

## Design

Each API accepts the existing RunPod-shaped request body (`{ "input": { ... } }`)
and returns RunPod-compatible `id` and `status` fields. n8n can therefore keep
its polling pattern. Each worker has a dedicated Redis queue and processes one
job at a time. Outputs continue to be read from and written to Cloudflare R2.

No host port is published. The APIs are reachable only from n8n over its
existing Docker network as `http://birthday-render-api:8080` and
`http://birthday-voice-api:8080`.

## Deploy on the VPS

1. Clone this repository in a dedicated directory, then create `.env` from
   `.env.example` and fill in the real values.
2. Discover the network attached to the existing n8n container and set
   `N8N_DOCKER_NETWORK` to that exact name.
3. Start the stack:

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs -f render-api render-worker
   ```

4. From the n8n container, verify the render API before changing a workflow:

   ```sh
   curl -H "Authorization: Bearer $WORKER_API_TOKEN" http://birthday-render-api:8080/health
   ```

## n8n changes after deployment

Only make these changes in a disabled workflow copy:

- FFmpeg submit node: `POST http://birthday-render-api:8080/v1/jobs`;
- FFmpeg status node: `GET http://birthday-render-api:8080/v1/jobs/{{ job id }}`;
- use `Authorization: Bearer <WORKER_API_TOKEN>` on both nodes;
- retain body and status polling logic unchanged.

The separate voice-removal caller should use the equivalent `birthday-voice-api`
URLs. Do not touch the karaoke-aligner nodes.

## Safety and rollback

Keep the RunPod nodes and original workflows unchanged until output comparison
passes. To roll back a copied flow, point its two URLs back to RunPod or leave it
disabled. Do not enable a copied Telegram trigger that shares the production bot.
