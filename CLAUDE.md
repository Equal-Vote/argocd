# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an **ArgoCD GitOps repository** for the [Equal Vote Coalition](https://equal.vote) Kubernetes cluster on Azure AKS. It manages all cluster workloads declaratively — changes pushed to `main` are automatically synced to the cluster.

The live ArgoCD UI is at `argocd.prod.equal.vote`.

## Architecture

### Bootstrap pattern

`application.yaml` is applied once manually to bootstrap the cluster. It creates two ArgoCD `Application` resources:

1. **`bootstrap-secrets`** — syncs the `secrets/` directory, which uses kustomize + ksops (SOPS-encrypted secrets) to create Kubernetes secrets.
2. **`bootstrap-cluster`** — syncs the `applications/` directory, which contains an `ApplicationSet` that discovers and deploys all apps.

### ApplicationSet (apps-of-apps)

`applications/applicationset.yaml` scans for every `applications/**/config.json` file and creates an ArgoCD `Application` for each one. Each `config.json` defines:
- The Helm chart source (`chartURL`, `chartName`, `revision`)
- Values pulled from this repo (`valuesURL`/`valuesRevision` pointing to `HEAD` of this repo)
- Destination namespace and cluster
- A `phase` label (`initial` → `core` → `post`) that controls rolling sync order

The matching `values.yaml` next to each `config.json` is the Helm values file for that application.

### Deployment phases

Apps sync in order by phase:
- **`initial`**: cert-manager, external-dns, ingress-nginx
- **`core`**: argocd (self-managed), loki
- **`post`**: keycloak, postgresql, star-server, discord-bot, alaska-rcv

### Secrets management

Secrets in `secrets/secrets.enc.yaml` are encrypted with SOPS/age. ArgoCD is configured with the ksops plugin (`kustomize.buildOptions: "--enable-alpha-plugins --enable-exec"`) to decrypt them at sync time.

### Disabled applications

`applications-disabled/` holds apps that are temporarily inactive (currently `kube-prometheus-stack`, `matomo`). Move a directory to `applications/` to enable it; move it back to disable.

## Adding or updating an application

1. **New app**: Create `applications/<name>/config.json` and `applications/<name>/values.yaml`. The ApplicationSet picks it up automatically on the next sync.
2. **Update a chart version**: Change `revision` in `config.json`.
3. **Update app config**: Edit `values.yaml`. Changes are picked up automatically on push to `main`.
4. **Image updates**: The `image.tag` in `values.yaml` is updated automatically by CI (commit messages like `Automatically updating bettervoting image.`).

## Image tagging convention

Applications using the `devopscoop/charts` generic `app` chart reference images via `image.repository` and `image.tag` in `values.yaml`. Tags use the format `sha-<short-sha>`.

## Loki/Grafana (logging)

Loki is in `applications-disabled/` by default (frugality mode). To use it:
1. Move `applications-disabled/loki` → `applications/loki`
2. Verify at `argocd.prod.equal.vote` that it syncs green
3. Port-forward: `kubectl port-forward svc/loki-grafana 80:80 -n monitoring`
4. Login: username and password are both `admin`

Useful LogQL queries:
```
# All star-server logs
{pod=~"star-server-app-.*"}

# 500 errors
{pod=~"star-server-app-.*"} |~ "status:50.+"
```