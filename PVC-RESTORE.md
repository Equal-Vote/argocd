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

| Namespace  | PVC Name            | Size | PV (auto-provisioned) |
| ---------- | ------------------- | ---- | --------------------- |
| `starvote` | `data-postgresql-0` | 8Gi  | `pvc-<new-uuid>`      |
| `keycloak` | `data-postgresql-0` | 8Gi  | `pvc-<new-uuid>`      |
| `loki`     | `data-loki-0`       | 10Gi | `pvc-<new-uuid>`      |

Record the PV names — you need to delete them (they point to the empty disks).

## 4. Identify Original (Deleted) Disks in the Backup Vault

The backup instances are named after the original disk UUIDs. List them all:

```bash
az backup container list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --backup-management-type AzureWorkload \
  --query "[].{name:name, status:properties.healthStatus}" \
  -o table
```

For each container (disk), list its recovery points and disk size:

```bash
az backup recoverypoint list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --backup-management-type AzureWorkload \
  --container-name "<disk-name>" \
  --item-name "<disk-name>" \
  --query "[*].{rp:name, time:properties.recoveryPointTime, size:properties.recoveryPointSizeInBytes}" \
  -o table
```

Get only the latest recovery point per disk:

```bash
for CONTAINER in $(az backup container list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --backup-management-type AzureWorkload \
  --query "[].name" -o tsv); do

  echo "=== $CONTAINER ==="
  az backup recoverypoint list \
    --vault-name equalvote-backup-vault \
    --resource-group equalvote \
    --backup-management-type AzureWorkload \
    --container-name "$CONTAINER" \
    --item-name "$CONTAINER" \
    --query "max_by([], &properties.recoveryPointTime).{rp:name, time:properties.recoveryPointTime, size:properties.recoveryPointSizeInBytes}" \
    -o tsv
done
```

## 5. Map Original Disks to Workloads by Size

Use the recovery point size (or original disk size) to identify each disk:

- **10 GiB** → Loki (`loki`)
- **8 GiB** → PostgreSQL or Keycloak PostgreSQL

For the two 8 GiB disks, distinguish them after restoring (see step 7), or
check the volume label/uuid against the cluster's old PV records.

## 6. Restore a Disk from Snapshot

For each original disk, create a new managed disk from its latest recovery
point:

```bash
DISK_NAME="<original-disk-uuid-from-step-4>"
RP_ID="<latest-rp-name-from-step-4>"
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

Repeat for each original disk (3 total).

## 7. Identify Which Restored Disk Belongs to Which Workload

Restored disks are offline (not attached). Mount one at a time to inspect its
content:

```bash
# Create a temporary VM (or use an existing node's mount). Simpler: attach to
# an existing node pod that has hostPath access.

# Or, inspect by creating a temporary PV/PVC + debug pod:
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-inspect
spec:
  capacity:
    storage: 10Gi     # match restored disk size
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-name>
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-inspect
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  volumeName: pv-inspect
  storageClassName: ""
---
apiVersion: v1
kind: Pod
metadata:
  name: pod-inspect
  namespace: default
spec:
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: pvc-inspect
  containers:
    - name: inspect
      image: busybox
      command: ["sh", "-c", "ls -la /data && sleep 10"]
      volumeMounts:
        - name: data
          mountPath: /data
EOF

kubectl logs pod-inspect
```

**Loki** contains `chunks`, `index`, `wal` directories.
**PostgreSQL** contains `PG_VERSION`, `base/`, `global/` directories.

Once identified, clean up the inspect pod/PV/PVC.

## 8. Delete Current PVCs and Their Empty Disks

For each workload, delete the current PVC (which cascades to delete the empty
Azure disk — the CSI driver's default reclaim policy is Delete):

```bash
# Delete PVCs (this also deletes the empty Azure disks)
kubectl delete pvc -n starvote data-postgresql-0
kubectl delete pvc -n keycloak data-postgresql-0
kubectl delete pvc -n loki data-loki-0

# Delete the orphaned PVs that were bound to those PVCs
kubectl delete pv <pv-name-from-step-3>     # starvote postgresql
kubectl delete pv <pv-name-from-step-3>     # keycloak postgresql
kubectl delete pv <pv-name-from-step-3>     # loki
```

> Confirm the empty Azure disks are gone: `az disk list -g MC_equalvote_equalvote_westus2 --query "[].name" -o table`

## 9. Create New PVs Backed by Restored Disks

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
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-uuid-for-postgresql>
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
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-uuid-for-keycloak>
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
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/<restored-disk-uuid-for-loki>
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-loki-0
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

## 10. Scale Workloads Back Up

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

## 11. Re-enable ArgoCD Sync

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
- **StatefulSet PVC naming.** Bitnami PostgreSQL creates PVCs named
  `data-<release>-0`. Confirm the exact name with `kubectl get pvc -A`.
