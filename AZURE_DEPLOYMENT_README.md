# Azure Container Apps & CI/CD Deployment Guide for Aasaan

This guide explains:
1. **Architecture**: How Aasaan runs as a containerized cloud application on Azure Container Apps with safe SQLite persistence.
2. **CI/CD Automation**: How to link your GitHub repository (even as a contributor working from a personal fork) so every push automatically deploys to Azure and shows the live deployment URL.

---

## Part 1: How Containerized Apps Are Implemented in Aasaan

Deploying an application with an embedded database like SQLite to modern cloud platforms (such as Azure Container Apps or Kubernetes) presents a unique challenge:

### The Cloud Challenge
- **Ephemeral Container Storage**: By default, container storage resets to zero whenever a container restarts, crashes, or is updated with a new revision.
- **Network Filesystems Break SQLite**: Azure Files uses **SMB/CIFS**. SQLite requires POSIX-compliant advisory locks (`fcntl`/`flock`) and shared memory mappings (`mmap`). Running SQLite directly on an SMB share results in lock contention, disk I/O errors, or database corruption.

### The Solution: Multi-Container Pod Architecture

Aasaan resolves this using Azure Container Apps' **multi-container pod pattern** paired with **two distinct storage volumes**:

```
 ┌────────────────────────── Azure Container Apps Pod ──────────────────────────┐
 │                                                                              │
 │  1. Init Container: "restore"                                                │
 │     • Command: python -m app.restore                                         │
 │     • Runs ONCE before the application containers start                      │
 │     • Checks /mnt/files/backups/ for the latest valid backup                 │
 │     • Copies it into /app/local/aasaan.db                                    │
 │     • Exits with status 0 to allow pod startup                               │
 │                                                                              │
 │  2. Main Container: "app" (FastAPI Engine)                                   │
 │     • Operates on /app/local/aasaan.db (Fast, local POSIX locking)          │
 │     • Serves HTTP / SSE requests on port 8000                                │
 │     • Saves uploaded learner assets to /mnt/files/uploads (Persistent)       │
 │     • Monitored via liveness (/health) and readiness (/health/ready) probes  │
 │                                                                              │
 │  3. Sidecar Container: "backup"                                              │
 │     • Command: python -m app.backup_loop                                     │
 │     • Runs continuously in the background                                    │
 │     • Takes an online SQLite backup snapshot every 5 minutes                 │
 │     • Stores verified snapshots in /mnt/files/backups                        │
 │                                                                              │
 │   ┌───────────────────────────┐         ┌───────────────────────────────┐    │
 │   │ EmptyDir Volume           │         │ Azure Files (SMB Share)       │    │
 │   │ Mount: /app/local         │         │ Mount: /mnt/files             │    │
 │   │ Fast ephemeral NVMe       │         │ Persistent cloud storage      │    │
 │   └───────────────────────────┘         └───────────────────────────────┘    │
 └──────────────────────────────────────────────────────────────────────────────┘
```

### Key Components

1. **EmptyDir Volume (`/app/local`)**:
   - An isolated temporary directory attached to the host node.
   - Houses the active SQLite database (`aasaan.db`) with full POSIX lock support, high read/write speeds, and zero SMB latency.
2. **Azure Files Volume (`/mnt/files`)**:
   - Persistent cloud storage backed by an Azure Storage Account (`Standard_LRS`).
   - Houses backups (`/mnt/files/backups`) and user file uploads (`/mnt/files/uploads`).
3. **Single Replica Constraint (`minReplicas: 1, maxReplicas: 1`)**:
   - Configured in single-revision mode to prevent multiple writers and guarantee database consistency.
4. **Automated TLS & Reverse Proxy Ingress**:
   - Azure Container Apps provisions free, auto-renewed HTTPS certificates.
   - Ingress terminates TLS and routes traffic to port `8000`.

---

## Part 2: Connecting GitHub to Azure for Automated Deployments (CI/CD)

### Understanding Permissions: Upstream vs. Personal Fork

- **Upstream (`OswaldShilo/Aasaan`)**: You are a contributor submitting Pull Requests. You cannot (and should not) add personal Azure credentials or secrets to upstream repository settings.
- **Your Fork (`NANDAVELAN/Aasaan`)**: **You are the Owner/Author**. You have full administrative rights to configure GitHub Actions, repository secrets, and deployment environments.

By setting up CI/CD on **your fork**, your personal Azure credentials stay 100% private, and your live cloud prototype updates automatically whenever you update your code.

---

### Step-by-Step Setup

#### Step 1: Configure Secrets in Your Fork

In your GitHub repository (**Settings > Secrets and variables > Actions**), add the following secrets:

| Secret Name | Value | Description |
| :--- | :--- | :--- |
| `AZURE_CREDENTIALS` | JSON string | Output of `az ad sp create-for-rbac` (or Azure Service Principal) |
| `ACR_NAME` | `aasaanacr` | Your Azure Container Registry name |
| `ACR_PASSWORD` | `****` | ACR Admin password (`az acr credential show`) |
| `GEMINI_API_KEY` | `****` | Google AI Studio API key |
| `AASAAN_ADMIN_TOKEN` | `****` | Secret admin bearer token |

---

#### Step 2: Configure the GitHub Deployment Environment

1. Navigate to your fork on GitHub: **Settings > Environments**.
2. Click **New environment** and enter the name: `production`.
3. Under **Environment URL**, enter your live Container App URL:
   ```text
   https://aasaan.purplemoss-02047b9c.centralindia.azurecontainerapps.io
   ```
4. Save the environment.

> **Result**: GitHub will now automatically display a **"Deployed to production"** badge on every commit and PR, complete with a direct link to the running application.

---

#### Step 3: Add the GitHub Actions Workflow

Create the workflow file at `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Azure Container Apps

on:
  push:
    branches:
      - main

# Automatically links the deployment to your GitHub Environment URL
environment:
  name: production
  url: https://aasaan.purplemoss-02047b9c.centralindia.azurecontainerapps.io

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Azure Login
        uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}

      - name: Log in to Azure Container Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ secrets.ACR_NAME }}.azurecr.io
          username: ${{ secrets.ACR_NAME }}
          password: ${{ secrets.ACR_PASSWORD }}

      - name: Build and Push Container Image
        run: |
          IMAGE_TAG="${{ secrets.ACR_NAME }}.azurecr.io/aasaan:${{ github.sha }}"
          docker build --platform linux/amd64 -t "$IMAGE_TAG" .
          docker push "$IMAGE_TAG"

      - name: Render and Update Container App
        env:
          RG: aasaan-rg
          APP: aasaan
          TAG: ${{ github.sha }}
          ACR: ${{ secrets.ACR_NAME }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          AASAAN_ADMIN_TOKEN: ${{ secrets.AASAAN_ADMIN_TOKEN }}
        run: |
          python infra/azure/render.py infra/azure/app.template.yaml infra/azure/app.yaml
          az containerapp update -g "$RG" -n "$APP" --yaml infra/azure/app.yaml
          rm -f infra/azure/app.yaml
```

---

### Everyday Workflow: How It Runs

```
   [Develop Feature] ──► Push to feature branch on fork ──► Open PR against upstream/main
                                                                    │
                                                                    ▼
   [Live Prototype]  ◄── Auto-Deploys via GitHub Actions ◄── Merge or sync fork main
```

1. **Feature Development**:
   - Create your feature branch, push to your fork, and submit a PR to `OswaldShilo/Aasaan`.
2. **Syncing Fork & Updating Live Prototype**:
   - Whenever you want your live deployment updated with new features, merge them to your fork's `main` branch (or click **"Sync fork"** on GitHub).
   - GitHub Actions triggers automatically.
   - Docker builds the new image tagged with the commit SHA (`github.sha`).
   - Azure Container Apps spins up the new revision with zero downtime.
   - GitHub updates the commit status with the live URL link.
