# Birthday workers for Hostinger

This independent Docker Compose stack hosts two CPU workloads currently handled
by RunPod:

- `render-*`: the 4K FFmpeg video and thumbnail renderer;
- `voice-*`: Demucs-based vocal removal / instrumental creation.

The render handler also supports a separate `render_slideshow` mode for themed
videos. It composes R2-hosted still images into a duration-matched video, adds
motion, transitions, timed ASS/text overlays, optional intro audio, and an
optional thumbnail. The existing `render` mode is unchanged.

The karaoke aligner intentionally remains on RunPod because its current handler
requires CUDA (`WhisperX` + Demucs).

## Design

Each API accepts the existing RunPod-shaped request body (`{ "input": { ... } }`)
and returns RunPod-compatible `id` and `status` fields. n8n can therefore keep
its polling pattern. Each worker has a dedicated durable Redis queue. A shared
lock permits only one CPU-heavy render or voice-removal job at a time, preserving
capacity for n8n and PostgreSQL on the VPS. Interrupted jobs are returned
to their queue after a worker restart. Outputs continue to be read from and
written to Cloudflare R2, while per-job temporary media is deleted after each job.

No host port is published. The APIs are reachable only from n8n over its
existing Docker network as `http://birthday-render-api:8080` and
`http://birthday-voice-api:8080`.

## Themed slideshow payload

Submit slideshow jobs to the same render API. `audio_duration_seconds` is a
planning hint; the worker verifies the downloaded audio with FFprobe before it
renders.

```json
{
  "input": {
    "mode": "render_slideshow",
    "jobId": "age-party-25",
    "music_key": "jobs/example/song.mp3",
    "audio_duration_seconds": 42.8,
    "image_keys": [
      "visual-assets/party/image-001.webp",
      "visual-assets/party/image-002.webp"
    ],
    "overlay_ass_key": "jobs/example/age-overlay.ass",
    "out_key": "jobs/example/final.mp4",
    "render": {
      "canvas": {"width": 3840, "height": 2160},
      "slideshow": {
        "fps": 30,
        "image_duration_seconds": 5,
        "transition_seconds": 0.6,
        "seed": 25
      },
      "text_events": [
        {
          "text": "HAPPY 25TH BIRTHDAY",
          "start_seconds": 0.4,
          "end_seconds": 4.5,
          "animation": "pop"
        }
      ],
      "thumbnail": {
        "enabled": true,
        "background_key": "visual-assets/party/image-001.webp",
        "name_text": {"text": "25TH"}
      }
    }
  }
}
```

At least one `image_keys` entry and one `music_key` are required. A supplied
`overlay_ass_key` is optional and is intended for word-aligned age highlights.

Workers use a separate egress network for R2 and source downloads. Redis remains
on an internal-only network. The default limits target a 4-vCPU / 16-GB Hostinger
KVM4. A single heavy job can use at most 2.5 vCPU, leaving capacity for n8n,
PostgreSQL, and the API containers. Benchmark these limits before production
cutover.

## Deploy on the VPS

1. Clone the `codex/hostinger-migration` branch in a dedicated directory, then
   create `.env` from `.env.example` and fill in the real values. The Docker
   build explicitly excludes `.env`.
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
