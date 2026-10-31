# PVC Restore from Azure Disk Backup

Procedure to restore PVCs from Azure Disk Backup snapshots after PVCs were
deleted and re-created, leaving new empty disks in place.

The backup vault (`equalvote-backup-vault`) still holds recovery points for the
**original** (deleted) disks — those are what we restore.

## 1. Suspend ArgoCD Sync

Prevent ArgoCD from reconciling during the restore. These apps are multi-source,
so use `kubectl patch`:

```bash
for APP in postgresql keycloak loki kube-prometheus-stack; do
  kubectl patch application "$APP" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}'
done
```

> Re-enable later with `--sync-policy automated --auto-prune=false --source-pos 0`.

## 2. Scale Down Workloads

Stop pods so PVCs can be swapped:

```bash
kubectl scale statefulset -n starvote postgresql --replicas=0
kubectl scale statefulset -n keycloak keycloak-postgresql --replicas=0
kubectl scale statefulset -n loki loki --replicas=0
```

> Scale back up after restore.

## 3. List Current PVCs and Their New (Empty) Disk IDs

Identify the empty PVCs that need to be replaced:

```bash
kubectl get pvc -A -o custom-columns=\
NAMESPACE:.metadata.namespace,\
NAME:.metadata.name,\
SIZE:.spec.resources.requests.storage,\
VOLUME:.spec.volumeName
```

Expected empty PVCs:

| Namespace  | PVC Name                     | Size | PV (auto-provisioned) |
| ---------- | ---------------------------- | ---- | --------------------- |
| `starvote` | `data-postgresql-0`          | 8Gi  | `pvc-<new-uuid>`      |
| `keycloak` | `data-keycloak-postgresql-0` | 8Gi  | `pvc-<new-uuid>`      |
| `loki`     | `storage-loki-0`             | 10Gi | `pvc-<new-uuid>`      |

Record the PV names — you need to delete them (they point to the empty disks).

## 4. Find the Backup Instance for Each PVC

The vault (`equalvote-backup-vault`) is a Data Protection backup vault — use
`az dataprotection` commands, not `az backup`.

Backup instances are named after the original disk (`pvc-<uuid>`), which says
nothing about the workload. The mapping lives in
[`tagged-disks.auto.tfvars`](https://github.com/Equal-Vote/terraform/blob/main/tagged-disks.auto.tfvars)
in the terraform repo: `tag-pvc-disks.sh` writes each disk's PVC as a
`# namespace/pvc` comment, from the tags the CSI driver puts on the disk, and
keeps the entry after the disk is deleted.

List every backup instance with its PVC and latest recovery point:

```bash
LABELS=$(curl -fsSL https://raw.githubusercontent.com/Equal-Vote/terraform/main/tagged-disks.auto.tfvars \
  | sed -n '/^disk_ids = \[/,/^\]/p' \
  | sed -nE 's#.*/disks/(pvc-[^"]*)", \# (.*)#\1 \2#p')

for INSTANCE in $(az dataprotection backup-instance list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --query "[].name" -o tsv); do

  RP=$(az dataprotection recovery-point list \
    --backup-instance-name "$INSTANCE" \
    --vault-name equalvote-backup-vault \
    --resource-group equalvote \
    --query "max_by([], &properties.recoveryPointTime).join(' ', [name, properties.recoveryPointTime])" -o tsv)

  PVC=$(awk -v i="$INSTANCE" '$1 == i { print $2 }' <<< "$LABELS")
  echo "$INSTANCE  ${PVC:-<no label>}  $RP"
done
```

Example output (from 2026-10-31, prometheus/alertmanager PVC names shortened):

```
pvc-eed94c08-a919-4ddf-8463-c7d5a49cdc45  kube-prometheus-stack/alertmanager-…-0  9cfde15e461344199b9d67381a98f8d8 2026-10-31T02:04:14.8742085Z
pvc-00a3f621-3d0d-4473-bab2-071cd9356775  fider/fider-db-1  937cb7b512d649cb8d11198e17e8d3a2 2026-10-31T02:03:46.4715662Z
pvc-56229c63-fd75-49d0-8f6b-7ce7d107bc68  keycloak/data-keycloak-postgresql-0  3e9d2f0953704fe49f52ff15795fd5dd 2026-10-31T02:04:12.5460269Z
pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba  starvote/data-postgresql-0  1acd1a9abdbd4034af1e51878f4e0ef4 2026-10-31T02:04:33.0604876Z
pvc-df9a66c8-ccec-435e-936b-5bf2f6745465  fider/fider-db-1  4e019c306ab041b1be2da085d4fceee9 2026-10-31T02:04:27.9989792Z
pvc-b3bbf7bd-fd95-4947-80aa-5ee343009001  loki/storage-loki-0  8440c67bea84483b8ecb1cf1c7d79557 2026-10-31T02:06:34.1259180Z
pvc-a144bae2-f4fd-4e6f-ae57-4f988ba4ca07  kube-prometheus-stack/prometheus-…-0  7bd39519b0a24dbaa58fb928b39fe467 2026-10-31T02:04:34.8144835Z
```

Note each workload's instance name and recovery point ID for the next step.

- **Several instances for one PVC** means the PVC was recreated at some point,
  each time with a new disk (above, `fider/fider-db-1` has two).
  - If the old disk was deleted, its instance stopped getting recovery points
    at that moment. Pick the instance whose latest recovery point is from just
    before the data loss.
  - If the old disk still exists (its PV was `Retain`), both instances keep
    getting recovery points. Find the disk that held the data with
    `kubectl get pv` (look for a `Released` PV with this claim) or
    `az disk list -g MC_equalvote_equalvote_westus2 --query "[?diskState=='Unattached'].name"`.
    You may not need a restore at all: skip step 5 and use that disk directly
    in step 7.
- **`<no label>`** means the disk was dropped from the list before it became
  append-only on 2026-10-30, or it had no CSI tags. Search the file's history
  (`git log -S <uuid> -p -- tagged-disks.auto.tfvars`), or restore it and
  inspect its contents as described in the
  [previous version of this doc](https://github.com/Equal-Vote/argocd/blob/4497bf552de5fdf28a5ad4d7ae2a783df27dee90/PVC-RESTORE.md#7-identify-which-restored-disk-belongs-to-which-workload).

## 5. Restore a Disk from Snapshot

For each workload, restore its recovery point to a new managed disk. The Data
Protection API uses a two-step process: initialize the restore request JSON,
then trigger the restore.

```bash
INSTANCE="<instance-name-from-step-4>"
RP_ID="<recovery-point-id-from-step-4>"
RESTORED_NAME="${INSTANCE}-restored"

# 1) Generate the restore request body
RESTORE_REQUEST=$(az dataprotection backup-instance restore initialize-for-data-recovery \
  --datasource-type AzureDisk \
  --restore-location westus2 \
  --source-datastore OperationalStore \
  --recovery-point-id "$RP_ID" \
  --target-resource-id "/subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/$RESTORED_NAME")

# 2) Trigger the restore (--no-wait returns immediately)
az dataprotection backup-instance restore trigger \
  --name "$INSTANCE" \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --restore-request-object "$RESTORE_REQUEST" \
  --no-wait
```

This creates a disk named `pvc-<uuid>-restored` in
`MC_equalvote_equalvote_westus2`. Check progress with:

```bash
az disk show -g MC_equalvote_equalvote_westus2 -n "$RESTORED_NAME" --query provisioningState
```

Repeat for each workload. The restored disk name tells you which workload it
belongs to: it's the instance name from step 4.

## 6. Delete Current PVCs and Their PVs

```bash
kubectl delete pvc -n starvote data-postgresql-0
kubectl delete pvc -n keycloak data-keycloak-postgresql-0
kubectl delete pvc -n loki storage-loki-0

kubectl delete pv <pv-name-from-step-3>     # starvote postgresql
kubectl delete pv <pv-name-from-step-3>     # keycloak postgresql
kubectl delete pv <pv-name-from-step-3>     # loki
```

Whether the empty Azure disks go too depends on each PV's reclaim policy:
`Delete` removes the disk, `Retain` (the `managed-csi-retain` class, or a PV
patched to `Retain`) keeps it. Once the restore is verified, delete any empty
disks that are left. For dynamically provisioned PVs the disk has the same
name as the PV:

```bash
az disk list -g MC_equalvote_equalvote_westus2 --query "[?diskState=='Unattached'].name" -o table
az disk delete -g MC_equalvote_equalvote_westus2 -n <pv-name-from-step-3>
```

## 7. Create New PVs Backed by Restored Disks

**PostgreSQL (starvote):**

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-restored-postgresql
spec:
  capacity:
    storage: 8Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-name-for-postgresql>
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-postgresql-0
  namespace: starvote
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 8Gi
  volumeName: pv-restored-postgresql
  storageClassName: ""
```

**Keycloak PostgreSQL:**

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-restored-keycloak-postgresql
spec:
  capacity:
    storage: 8Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-name-for-keycloak>
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-keycloak-postgresql-0
  namespace: keycloak
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 8Gi
  volumeName: pv-restored-keycloak-postgresql
  storageClassName: ""
```

**Loki:**

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-restored-loki
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-name-for-loki>
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: storage-loki-0
  namespace: loki
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  volumeName: pv-restored-loki
  storageClassName: ""
```

Apply them:

```bash
kubectl apply -f restored-pvcs.yaml
```

## 8. Scale Workloads Back Up

```bash
kubectl scale statefulset -n starvote postgresql --replicas=1
kubectl scale statefulset -n keycloak keycloak-postgresql --replicas=1
kubectl scale statefulset -n loki loki --replicas=1
```

Verify each pod mounts the restored PVC and starts without errors:

```bash
kubectl logs -n starvote postgresql-0 --tail=20
kubectl logs -n keycloak keycloak-postgresql-0 --tail=20
kubectl logs -n loki loki-0 --tail=20
```

## 9. Re-enable ArgoCD Sync

```bash
for APP in postgresql keycloak loki kube-prometheus-stack; do
  kubectl patch application "$APP" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":false,"selfHeal":true}}}}'
done
```

## Caveats

- **Crash-consistent snapshots.** Azure Disk Backup does not quiesce the
  filesystem. PostgreSQL may require WAL replay on startup; this usually
  succeeds but verify logs.
- **Region / zone.** Restored disks land in the same region (West US 2). If
  your AKS node pool uses availability zones, specify one or add a topology
  constraint to the PV.
- **Reclaim policy.** The PV uses `Retain` to prevent accidental deletion.
- **`storageClassName: ""`** on the PVC is required to bind to an existing PV.
- **StatefulSet PVC naming.** PVC names come from each chart's
  `volumeClaimTemplates` (`data-postgresql-0`, `data-keycloak-postgresql-0`,
  `storage-loki-0`). Confirm the exact names with `kubectl get pvc -A`; the
  labels from step 4 show the names at the time the disk was created.
