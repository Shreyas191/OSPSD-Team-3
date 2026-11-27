# course-web

The interface for Course Autopilot (HW3 Milestone 2, Level 2; DESIGN.md decision P4): a small
FastAPI app that renders pages with Jinja templates. It holds no product rule and calls no
provider or database; pages get their data from the agent and API services over HTTP.

This first version is the skeleton (#94): a start page and `GET /health`, deployed as
`course-web-<env>` by OpenTofu (`infra/web.tf`) and the pipeline (`scripts/deploy_web.sh`).
Sign-in (#96) and the S1 pages (#95) come next.

```bash
uv run python web_main.py        # http://127.0.0.1:8002
```

| Setting | Meaning | Default |
|---|---|---|
| `HOST`, `PORT` | Address to serve on | `127.0.0.1`, `8002` |
| `RELEASE` | Commit shown in the page footer and `/health` (set by the image build) | `unknown` |
