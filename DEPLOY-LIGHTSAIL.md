# Deploy Aasaan on one AWS Lightsail VM

One VM, Docker Compose, three containers: **app** (FastAPI + SQLite), **caddy** (automatic HTTPS) and **backup** (SQLite-safe backups).
SQLite means exactly **one** app instance. Do not scale it.

```
Internet -> :80/:443 Caddy -> app:8000 (private, never published)
                              app + backup share the volume aasaan-data (/app/data: aasaan.db, uploads/, backups/)
```

Tested sizing: 2 vCPU, 2 GB RAM, Ubuntu 24.04 (x86-64 or ARM64; both work, the image is plain Python). Offline OCR (Indra) is off in this build.

## 1. Lightsail setup (AWS console)

1. **Static IP:** Networking, create a static IP, attach it to the instance (a plain public IP changes on stop/start).
2. **Firewall (Networking tab, IPv4 rules):** allow `22` (SSH, restrict to your IP if you can), `80`, `443` TCP, and `443` UDP (HTTP/3). Nothing else. Port 8000 stays closed.
3. **Domain:** point an `A` record (for example `aasaan.example.com`) at the static IP. No domain? Use `<ip-with-dashes>.sslip.io`, for example `35-154-40-208.sslip.io`, which resolves to that IP and still gets a real certificate.
4. **Snapshots:** enable automatic daily snapshots for the instance. This is your off-server copy of the whole disk.

## 2. Prepare the VM (once)

```bash
ssh -i "$HOME/.ssh/aasaan_lightsail" ubuntu@<static-ip>

uname -m && free -h && docker --version && docker compose version     # check Docker is there

# 2 GB of swap so a build or a memory spike cannot kill the box
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo usermod -aG docker ubuntu     # log out and back in once
```

## 3. Get the code and the secrets

```bash
git clone https://github.com/OswaldShilo/Aasaan.git && cd Aasaan
cp .env.example .env && chmod 600 .env
nano .env
```

Set at least:

| Key | Value |
|---|---|
| `GEMINI_API_KEY` | required |
| `AASAAN_DOMAIN` | your domain, no `https://` |
| `AASAAN_ADMIN_TOKEN` | a long random string (`openssl rand -hex 24`); without it admin endpoints answer only to localhost |
| `AASAAN_BACKUP_EVERY_MIN` | `1440` is daily (already in the template) |

Optional keys (OpenRouter, Sarvam, Jev, YouTube, ...) are listed in `.env.example`. `.env` is git-ignored and excluded from the image; keep it that way.

## 4. Start

```bash
docker compose up -d --build
docker compose ps                                    # app should become "healthy"
curl -s https://<your-domain>/health                 # {"ok":true}
curl -s https://<your-domain>/health/ready           # {"ok":true}  (database writable)
curl -s https://<your-domain>/api/health/models      # each model once: a wrong key or id shows up here
```

The first build takes a few minutes. The first request to a new domain waits a few seconds while Caddy gets the certificate.

## 5. Smoke test

Open the site, create a notebook, and watch chapter 1 appear (this exercises SSE through Caddy). Then:

```bash
docker compose logs -f app | head -50
docker compose exec app python -c "from app.backup_loop import run_backup_once as r; print(r())"   # optional: force a backup now
docker compose exec app ls -la /app/data/backups
```

## Backups

- The **backup** container runs `python -m app.backup_loop`: SQLite's online-backup API on the live database (never a file copy), an integrity check on the copy, an atomic rename, then pruning. Files are `aasaan-YYYY-MM-DD-HHMM.db` in `/app/data/backups` (same volume). Backups older than `AASAAN_BACKUP_KEEP_DAYS` (14) are deleted, but the newest 3 are always kept.
- A backup on the same disk does not survive losing the disk. Keep the **Lightsail snapshots** on, and copy a backup off the box now and then:

```bash
docker compose cp backup:/app/data/backups ./backups-copy
```

- Optional later phase: sync `/app/data/backups` to S3 (a small IAM user limited to one bucket, `aws s3 sync`). Not set up here.
- On-demand backup over HTTP: `curl -X POST -H "X-Admin-Token: $TOKEN" https://<domain>/api/admin/backup`.

## Restore

The app container runs `python -m app.restore` before it starts. If **no database exists**, it restores the newest valid backup automatically; if one exists, it does nothing. To restore deliberately:

```bash
docker compose stop app backup
docker compose run --rm --no-deps app sh -c 'ls /app/data/backups; rm -f /app/data/aasaan.db /app/data/aasaan.db-wal /app/data/aasaan.db-shm; python -m app.restore'
docker compose up -d
```

Test this on a throwaway notebook before you need it.

## Update and rollback

```bash
cd ~/Aasaan && git pull
docker tag aasaan:latest aasaan:previous           # keep the working image
docker compose up -d --build
curl -s https://<your-domain>/health/ready
```

Roll back:

```bash
git checkout <previous-commit>
docker tag aasaan:previous aasaan:latest && docker compose up -d --no-build
```

The database is in the volume, so rebuilds never touch it. If a release changed the schema and you must go back, also restore the matching backup (above).

## Operations

- `docker compose logs --tail=100 app` / `caddy` / `backup`
- `docker compose restart app`
- `docker system df` and `docker image prune -f` now and then; the disk is 60 GB.
- Memory: the app is capped at 1.2 GB, Caddy 192 MB, backup 256 MB. Watch `docker stats --no-stream`.
- Never run `docker compose down -v`: it deletes the volumes, including the database.

## Costs

- **AWS (fixed):** the Lightsail 2 GB plan is about $12/month (it bundles the disk, static IP and data transfer; check the current price). Snapshots cost a few cents per GB per month. A domain is about $10-15/year, or use sslip.io for free.
- **AI APIs (separate, usage-based):** Gemma through Google AI Studio and the OpenRouter `:free` models are free but rate-limited; paid models bill per token. Sarvam, YouTube, Jina and Firecrawl have their own free tiers. Set budgets in each provider's console.
