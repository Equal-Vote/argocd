# AGENTS.md — Equal Vote ArgoCD

**Pure GitOps YAML repo.** No code, no tests, no CI pipelines, no build system. ArgoCD auto-syncs on `git push`.

> **Pre-commit hooks** are managed via `../../devopscoop/dotfiles/3uzbcqje/.pre-commit-config.yaml`. The `check-yaml` hook will reject Go template expressions (`{{...}}`) in raw YAML files. Use `SKIP=check-yaml git commit` when editing template-heavy YAML. Do **not** use `git commit --no-verify` without asking permission first.

> **No commits or pushes without explicit user approval.** Wait for the user to say "commit" or "push" before staging, committing, or pushing anything.

## Bootstrap (new cluster)

```sh
helm repo add argo https://argoproj.github.io/argo-helm
kubectl create ns argocd
helm install argocd argo/argo-cd --namespace argocd --version 6.7.12
kubectl apply -f application.yaml
```

`application.yaml` creates two bootstrap Apps: `bootstrap-secrets` (kustomize+SOPS) and `bootstrap-cluster` (ApplicationSet). Everything else, cert-manager included, comes from the ApplicationSet.

## Phases (labels, not ordering)

Every app carries a `phase` label from its `config.json`:

- `initial` — cert-manager, ingress-nginx, external-dns
- `core` — argocd, loki, fluent-bit, kube-prometheus-stack
- `post` — keycloak, postgresql, star-server, alaska-rcv, discord-bot

These are for grouping and the ArgoCD UI. They **do not** order syncs. The
RollingSync strategy that consumed them was removed: the only cross-app
dependency in this cluster is cnpg-operator's CRDs before fider-db's
`postgresql.cnpg.io` Cluster, and the template's `retry` policy covers that.
Nothing else needs sync-time ordering — Ingresses, Certificates and database
connections all resolve at runtime.

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

- `automated.selfHeal: true` is live on every generated app. It used to be inert:
  `strategy: RollingSync` set `automated.enabled: false` and drove syncs itself.
- `retry` (5 attempts, 15s doubling to 5m) covers the cnpg-operator → fider-db CRD
  dependency the phases used to enforce.
- `prune: false` on **all** apps (universal default to protect PVCs)
- `syncOptions: [CreateNamespace=true, ServerSideApply=true]` on all apps
- `argocd.argoproj.io/compare-options: ServerSideDiff=true` annotation on all apps.
  ServerSideDiff is a **compare** option, not a sync option — it is read only from
  this annotation or from `controller.diff.server.side` in `argocd-cmd-params-cm`,
  and is silently ignored if placed in `syncOptions`.

## Important defaults

- All apps target `in-cluster` (same AKS cluster)
- cert-manager must stay in `initial`: nothing in that phase requests a Certificate,
  and its CRDs have to exist before the `core` / `post` apps whose Ingresses trigger
  ingress-shim. It also carries its own CRDs (`crds.enabled: true`).
- `applications-disabled/matomo` is the only disabled app
- `local/` and `clusterissuer.yaml` are gitignored
