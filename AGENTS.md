# AGENTS.md — Equal Vote ArgoCD

**Pure GitOps YAML repo.** No code, no tests, no CI pipelines, no build system. ArgoCD auto-syncs on `git push`.

> **Pre-commit hooks** are managed via `../../devopscoop/dotfiles/3uzbcqje/.pre-commit-config.yaml`. The `check-yaml` hook will reject Go template expressions (`{{...}}`) in raw YAML files. Use `SKIP=check-yaml git commit` when editing template-heavy YAML. Do **not** use `git commit --no-verify` without asking permission first.

## Bootstrap (new cluster)

```sh
helm repo add argo https://argoproj.github.io/argo-helm
kubectl create ns argocd
helm install argocd argo/argo-cd --namespace argocd --version 6.7.12
kubectl apply -f application.yaml
```

`application.yaml` creates three bootstrap Apps: `bootstrap-secrets` (kustomize+SOPS), `bootstrap-cluster` (ApplicationSet), and `cert-manager` (Helm, separate because it must run first).

## Deploy phases (ordered)

Defined in `applications/applicationset.yaml`. Apps are rolled in three phases:

1. `initial` — ingress-nginx, external-dns
1. `core` — argocd, loki, fluent-bit, kube-prometheus-stack
1. `post` — keycloak, postgresql, star-server, alaska-rcv, discord-bot

Add a new app by creating `applications/<name>/config.json` + `values.yaml`. The ApplicationSet Git file generator picks it up automatically.

## Enable / disable apps

Use `utils/matoMOVE.sh` (commits + pushes + optionally deletes namespace):

```sh
bash utils/matoMOVE.sh <app_name> disable   # moves to applications-disabled/
bash utils/matoMOVE.sh <app_name> enable    # moves back to applications/
```

## Secrets

Encrypted with SOPS + Azure Key Vault (`equalvote-argocd`). The `secrets/` dir uses Kustomize + ksops plugin. Edit the decrypted file, then:

```sh
sops --encrypt secrets/secrets.enc.yaml > secrets/secrets.enc.yaml
```

## ClusterIssuer

`clusterissuer.yaml` is **gitignored**. Generate it from template:

```sh
export EMAIL_ADDRESS=gmail@evanstucker.com
envsubst < utils/clusterissuer.template.txt > clusterissuer.yaml
kubectl apply -f clusterissuer.yaml
```

## Azure resource names

- AKS cluster: `equalvote` in resource group `equalvote`
- Key Vault: `equalvote-argocd`
- Managed identity (external-dns): `equalvote-identity`
- Managed identity (cert-manager): `cert-manager`
- Managed identity (workload identity for ArgoCD Key Vault access): `argocd`

Setup scripts: `utils/workload-identity.sh`, README sections for external-dns and cert-manager identities.

## Sync policy

Defined per-app in `config.json` via the `prune` field. Managed by ApplicationSet template at `applications/applicationset.yaml:30`.

- All apps **auto-sync** with `selfHeal: true`
- `prune: false` on stateful apps with PVCs: **keycloak**, **loki**, **postgresql**
- `prune: true` on all other apps
- cert-manager bootstrap app: `selfHeal: false` (defined in `application.yaml:67`)
- `syncOptions: [CreateNamespace=true, ServerSideApply=true]` on all apps

## Important defaults

- All apps target `in-cluster` (same AKS cluster)
- `applications-disabled/matomo` is the only disabled app
- `local/` and `clusterissuer.yaml` are gitignored
