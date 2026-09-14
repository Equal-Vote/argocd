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

The vault (`equalvote-backup-vault`) is a Data Protection backup vault — use
`az dataprotection` commands, not `az backup`.

List all backup instances (named after the original disk UUIDs):

```bash
az dataprotection backup-instance list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --query "[].{name:name, status:properties.currentProtectionState}" \
  -o table
```

Expected output — three original disks that were being backed up before deletion:

```
Name                                      Status
----------------------------------------  --------------------
pvc-3cd6e8ba-fb7f-4031-a69d-e5cd96e8efba  ProtectionConfigured
pvc-56229c63-fd75-49d0-8f6b-7ce7d107bc68  ProtectionConfigured
pvc-7d5e2740-7f0a-4115-acbe-b84975edf773  ProtectionConfigured
```

For each backup instance, find the latest recovery point and the disk size
(stored as JSON in the recovery point metadata):

```bash
for INSTANCE in $(az dataprotection backup-instance list \
  --vault-name equalvote-backup-vault \
  --resource-group equalvote \
  --query "[].name" -o tsv); do

  RP_ID=$(az dataprotection recovery-point list \
    --backup-instance-name "$INSTANCE" \
    --vault-name equalvote-backup-vault \
    --resource-group equalvote \
    --query "max_by([], &properties.recoveryPointTime).name" -o tsv)

  META=$(az dataprotection recovery-point show \
    --backup-instance-name "$INSTANCE" \
    --vault-name equalvote-backup-vault \
    --resource-group equalvote \
    --recovery-point-id "$RP_ID" \
    --query "properties.recoveryPointDataStoresDetails[0].metaData" -o tsv)

  SIZE=$(echo "$META" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())[0]
print(f\"{d['sizeBytes'] / 1024**3:.0f}GiB\")
")

  echo "$INSTANCE  latest=$RP_ID  size=$SIZE"
done
```

## 5. Map Original Disks to Workloads by Size

The recovery point metadata reveals the disk size. Use it to identify each
workload:

| Size  | Workload                    | Namespace  |
| ----- | --------------------------- | ---------- |
| 8GiB  | PostgreSQL (starvote)       | `starvote` |
| 8GiB  | Keycloak bundled PostgreSQL | `keycloak` |
| 10GiB | Loki                        | `loki`     |

> The two 8GiB disks are both Bitnami PostgreSQL and cannot be distinguished by
> size alone. After restoring one, mount it to inspect (step 7) — **PostgreSQL**
> contains `PG_VERSION`, `base/`, `global/`; **Keycloak** contains a `keycloak`
> database in `base/`.

## 6. Restore a Disk from Snapshot

For each original disk, restore its latest recovery point to a new managed disk.
The Data Protection API uses a two-step process: initialize the restore request
JSON, then trigger the restore.

```bash
INSTANCE="<original-disk-uuid-from-step-4>"
RP_ID="<latest-rp-id-from-step-4>"
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

Repeat for each original disk (3 total).

## 7. Identify Which Restored Disk Belongs to Which Workload

Mount the disk read-only with a busybox pod and inspect its content without
writing anything.

**Loki (10GiB):** contains `chunks/`, `index/`, `wal/` directories — just `ls`:

```bash
RESTORED_DISK="<loki-restored-disk-name>"

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-inspect
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/${RESTORED_DISK}
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-inspect
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
  name: pg-inspect
spec:
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: pvc-inspect
        readOnly: true
  containers:
    - name: inspect
      image: busybox
      command: ["ls", "/data"]
      volumeMounts:
        - name: data
          mountPath: /data
          readOnly: true
  restartPolicy: Never
EOF

kubectl logs pg-inspect
kubectl delete pod,pvc,pv pg-inspect pvc-inspect pv-inspect
```

**PostgreSQL (8GiB):** both disks have the same directory layout, but the
database names are embedded in the `pg_database` catalog file (`global/1260`).
Use `strings` — no server startup, no writes:

```bash
RESTORED_DISK="<postgres-restored-disk-name>"

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-inspect
spec:
  capacity:
    storage: 8Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: disk.csi.azure.com
    volumeHandle: /subscriptions/86f3145a-48cc-4255-8757-dd3104d15e57/resourceGroups/MC_equalvote_equalvote_westus2/providers/Microsoft.Compute/disks/${RESTORED_DISK}
    fsType: ext4
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-inspect
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 8Gi
  volumeName: pv-inspect
  storageClassName: ""
---
apiVersion: v1
kind: Pod
metadata:
  name: pg-inspect
spec:
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: pvc-inspect
        readOnly: true
  containers:
    - name: inspect
      image: busybox
      command:
        - sh
        - -c
        - strings /data/global/1260 | grep -v '^[0-9]*$' | sort -u
      volumeMounts:
        - name: data
          mountPath: /data
          readOnly: true
  restartPolicy: Never
EOF

kubectl logs pg-inspect
kubectl delete pod,pvc,pv pg-inspect pvc-inspect pv-inspect
```

Identify each disk from the output:

| `strings` output contains | This disk belongs to             |
| ------------------------- | -------------------------------- |
| `starvote`                | PostgreSQL (`starvote`)          |
| `bitnami_keycloak`        | Keycloak PostgreSQL (`keycloak`) |

Repeat for the other 8GiB disk.

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
  storageClassName: ""
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
  storageClassName: ""
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
  storageClassName: ""
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
