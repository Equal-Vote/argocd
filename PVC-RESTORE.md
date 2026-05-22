# PVC Restore from Azure Disk Backup

Procedure to restore PVCs from Azure Disk Backup snapshots after inadvertent deletion/recreation.

## 1. Suspend ArgoCD Sync

Prevent ArgoCD from detecting the manual PV/PVC changes and attempting to reconcile (even with `prune: false`, auto-sync can still cause issues).

These apps are multi-source, so `argocd app set` requires `--source-pos`. Use `kubectl patch` instead:

```bash
for APP in postgresql keycloak loki kube-prometheus-stack; do
  kubectl patch application "$APP" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}'
done
```

> Re-enable sync after restore by restoring the original syncPolicy, or with `argocd app set <app> --sync-policy automated --auto-prune=false --source-pos 0` for multi-source apps.

## 2. Disk to Workload Mapping

Run this in the cluster to map Azure disk IDs to PVCs:

```bash
kubectl get pv -o json | jq -r '
  .items[] | select(.spec.azureDisk or .spec.csi.driver == "disk.csi.azure.com") |
  "\(.metadata.name) → \(.spec.claimRef.namespace)/\(.spec.claimRef.name)  size=\(.spec.capacity.storage)"'
```

Expected mapping (from repo config):

| Disk UUID                                  | Workload                    | Namespace  | PVC Name (Bitnami pattern) | Size |
| ------------------------------------------ | --------------------------- | ---------- | -------------------------- | ---- |
| `pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba` | PostgreSQL (starvote)       | `starvote` | `data-postgresql-0`        | 8Gi  |
| `pvc-56229c63-fd75-49d0-8f6b-7ce7d107bc68` | Keycloak bundled PostgreSQL | `keycloak` | `data-postgresql-0`        | 8Gi  |
| `pvc-7d5e2740-7f0a-4115-acbe-b84975edf773` | Loki                        | `loki`     | `loki-<statefulset>-0`     | 10Gi |

> Verify PVC names with `kubectl get pvc -A` — Bitnami charts name PVCs `data-<release>-<replica>-0`.

## 3. Find the Latest Recovery Point

```bash
DISKS=(
  "pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba"
  "pvc-56229c63-fd75-49d0-8f6b-7ce7d107bc68"
  "pvc-7d5e2740-7f0a-4115-acbe-b84975edf773"
)

for DISK in "${DISKS[@]}"; do
  echo "=== $DISK ==="
  az backup recoverypoint list \
    --vault-name equalvote-backup-vault \
    --resource-group equalvote \
    --backup-management-type AzureWorkload \
    --container-name "$DISK" \
    --query "max_by([], &properties.recoveryPointTime).{name:name, time:properties.recoveryPointTime}" \
    -o tsv
done
```

## 4. Restore a Disk from Snapshot

For each disk to restore, create a new managed disk from the latest recovery point:

```bash
DISK_NAME="pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba"
RP_ID="<recovery-point-name-from-above>"
RESTORED_DISK_NAME="${DISK_NAME}-restored"
NODE_RG="MC_equalvote_equalvote_westus2"

az backup restore restore-disks \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --container-name "$DISK_NAME" \
  --item-name "$DISK_NAME" \
  --rp-name "$RP_ID" \
  --target-resource-group "$NODE_RG" \
  --storage-account "" \
  --restore-to-managed-disk \
  --disk-name "$RESTORED_DISK_NAME"
```

This creates a disk named `pvc-<uuid>-restored` in `MC_equalvote_equalvote_westus2`.

## 5. Create PV + PVC Backed by Restored Disk

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
  storageClassName: managed-csi
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba-restored
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
  storageClassName: managed-csi
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/pvc-56229c63-fd75-49d0-8f6b-7ce7d107bc68-restored
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-postgresql-0
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
  storageClassName: managed-csi
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/pvc-7d5e2740-7f0a-4115-acbe-b84975edf773-restored
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: loki-<statefulset>-0  # replace with actual name from `kubectl get pvc -n loki`
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

Apply and restart the pod:

```bash
kubectl apply -f restore-<workload>.yaml
kubectl delete pod -n <namespace> <pod-name>
```

The pod will re-create and pick up the restored PV.

## 6. Re-enable ArgoCD Sync

Once all PVCs are restored and pods are healthy, re-enable auto-sync:

```bash
for APP in postgresql keycloak loki kube-prometheus-stack; do
  kubectl patch application "$APP" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":false,"selfHeal":true}}}}'
done
```

The exact syncPolicy values should match what's in `applications/<app>/config.json`.

## Caveats

- **Crash-consistent snapshots.** Azure Disk Backup does not quiesce the filesystem. PostgreSQL data may require WAL replay on startup, which usually succeeds. For critical restores, consider `pg_start_backup()` / `pg_stop_backup()` during the backup window.
- **Region / zone.** Restored disks land in the same region (West US 2). If your AKS node pool uses availability zones, you may need to specify a zone in the restore command or create the PV with a `topology` constraint.
- **Orphaned disks.** If the PVC was re-created by Helm, the old disk (with original data) still exists in `MC_equalvote_equalvote_westus2` but is no longer bound. Verify which disk is current before restoring.
- **Retain policy.** The PV uses `persistentVolumeReclaimPolicy: Retain` to prevent accidental deletion.
- **`storageClassName: ""`** on the PVC is required to bind to an existing PV.
