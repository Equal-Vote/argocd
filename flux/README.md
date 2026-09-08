# Flux, alongside ArgoCD

A Flux translation of one app — `fider` — living next to the ArgoCD config it
will eventually replace. `fider` is first because it is the only app in the
cluster where a mistake costs nothing (see [Why fider](#why-fider)).

## Status: inert

Nothing here does anything until someone runs `flux bootstrap`. ArgoCD cannot
see this directory:

- the `applications` ApplicationSet generator globs `applications/**/config.json`
- the `bootstrap-cluster` Application syncs `path: applications`

Both are scoped to `applications/`, so `flux/` is invisible to ArgoCD, and this
PR is safe to merge at any time without changing cluster behaviour. It does not
remove `applications/fider/config.json` or `applications/fider-db/config.json` —
that is the cutover, and it is a separate change.

## Why fider

It is publicly served at `feedback.prod.equal.vote` but **not reachable from the
product** — `grep -rn 'fider\|feedback\.prod' packages/frontend/src packages/backend/src`
in the bettervoting repo returns nothing. So it is the one app where the blast
radius of getting a migration wrong is approximately zero.

That makes it worth using to rehearse the *hard* path rather than to dodge it.
`fider` happens to exercise three of the four blockers in the full migration:

| Blocker | Why fider hits it | Also needed for |
| --- | --- | --- |
| OCI chart from `devopscoop` | uses `devopscoop/charts/app` | `star-server`, `alaska-rcv`, `discord-bot` |
| Helm adoption | real PVC + CNPG cluster to adopt in place | `postgresql`, `keycloak` |
| SSA field handover | ArgoCD owns fields as `argocd-controller` | every app |

Note the `devopscoop` registry turned out to be **anonymously pullable** — see
below — so that row is a shape to rehearse, not an obstacle.

## Prerequisites

1. **`flux bootstrap`**, e.g.
   `flux bootstrap github --owner=Equal-Vote --repository=argocd --path=./flux/clusters/equalvote`.
   Adds four controllers in `flux-system`. Note the cluster is already firing
   `KubeMemoryOvercommit`, so budget for the extra requests.
2. **Make removal non-destructive.** See below.

No registry credentials are needed. `registry.gitlab.com/devopscoop/charts/app`
serves anonymous pulls: a token from `gitlab.com/jwt/auth` with no credentials
lists all tags and fetches the manifests for both `0.11.0` (fider) and `0.8.2`
(the other three apps). Verified 2026-09-08.

## ⚠️ Removing an app from the ApplicationSet currently destroys its data

This is true today, for every app, independent of Flux:

```
ApplicationSet syncPolicy:      (none)   <- no preserveResourcesOnDeletion
fider / fider-db finalizers:    resources-finalizer.argocd.argoproj.io
PV pvc-df9a66c8 (Bound, live):  Delete
PV pvc-00a3f621 (Released):     Retain   <- the OLD volume from the Aug incident
```

Deleting a `config.json` deletes the Application, the finalizer cascades, and
ArgoCD deletes the workloads — including the CNPG Cluster and its PVC, whose PV
then reclaims. The `Retain` policy applied after the August CNPG incident
protects the *previous* volume, not the one in use since the 08-29 rebuild.

Before any cutover, do one of:

- set `preserveResourcesOnDeletion: true` on the ApplicationSet `syncPolicy`
  (preferred — fixes this for every app at once), or
- `kubectl patch pv pvc-df9a66c8-... -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'`, or
- delete the Application with `--cascade=false` before removing its `config.json`

Verify the *bound* PV is the protected one, not an old `Released` entry.

## Cutover

Ordering is the whole game: **detach from ArgoCD, then let Flux adopt.** Never
both live — two controllers server-side-applying the same objects will fight
over field ownership indefinitely.

1. Confirm prerequisites, especially the registry pull.
2. Apply the deletion-safety fix above.
3. Stamp the existing resources so Helm adopts rather than reinstalls. ArgoCD
   renders charts and applies the manifests, so **no Helm release exists in the
   cluster** and `helm upgrade --install` would otherwise try a fresh install and
   collide. On every object the two charts own:
   ```
   kubectl -n fider annotate <kind>/<name> \
     meta.helm.sh/release-name=fider meta.helm.sh/release-namespace=fider
   kubectl -n fider label <kind>/<name> app.kubernetes.io/managed-by=Helm
   ```
   (`release-name=fider-db` for the CNPG `Cluster` and its objects.)
4. Detach ArgoCD: remove `applications/fider/config.json` and
   `applications/fider-db/config.json` in a separate PR.
5. Let Flux reconcile. Because chart versions are pinned to what ArgoCD runs,
   the resulting `helm upgrade` should be a no-op.
6. Watch for SSA conflicts against the leftover `argocd-controller` field
   manager. Once ArgoCD is no longer reconciling these objects the stale
   `managedFields` entries are harmless, but the first apply may need forcing.
7. Only after one clean reconcile, flip `driftDetection.mode` from `warn` to
   `enabled` in both HelmReleases.

## Rollback

Restore the two `config.json` files. ArgoCD recreates the Applications and
re-adopts the resources. Suspend Flux first so they do not fight:

```
flux suspend helmrelease fider fider-db -n fider
```

Because fider carries no data anyone depends on, `kubectl delete ns fider`
followed by a fresh install from either system is also a legitimate recovery.

## Deliberately not done here

- **Secrets stay with ArgoCD.** `fider` (JWT_SECRET, EMAIL_SMTP_PASSWORD) and
  `fider-db-creds` come from the standalone `bootstrap-secrets` app, which is
  *not* part of the ApplicationSet and is unaffected by the cutover. Moving them
  to Flux SOPS needs Azure Key Vault workload identity for `kustomize-controller`,
  which cannot be done in git alone. Separate step.
- **`cnpg-operator` stays with ArgoCD** (phase `core`). Flux cannot express
  `dependsOn` across to an ArgoCD Application. The operator is already running,
  so this is a documented gap, not an ordering bug.
- **No other app is touched.**

## Known gaps

- The `devopscoop` chart is public but third-party. It is a generic app wrapper
  rendering four objects (Deployment, Service, Ingress, ServiceAccount) from a
  values file, shared by fider, star-server, alaska-rcv and discord-bot. Nothing
  about fider requires it.

  The risk is continuity, not access: `be16440` records that this chart has
  already been moved once, "migrated from the now-deprecated dedevsecops org".
  Version drift is live too — fider pins `0.11.0` while the other three pin
  `0.8.2`. Vendoring the 10KB tarball into this repo, or replacing it with a
  kustomize base (more idiomatic under Flux anyway), would remove the dependency
  cheaply. Out of scope here.
- `flux/clusters/equalvote/flux-system/` is a placeholder; `flux bootstrap`
  writes the real controller manifests there.
- Nothing here has been validated against a live cluster — no `flux` or
  `kustomize` CLI is available in the authoring environment. Dry-run with
  `flux build kustomization apps --path ./flux/apps` before trusting it.
