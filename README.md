# Bootstrapping

Based on https://artifacthub.io/packages/helm/argo/argo-cd.

Installing ArgoCD in the cluster via Helm:

```
helm repo add argo https://argoproj.github.io/argo-helm
k create ns argocd
helm install argocd argo/argo-cd --namespace argocd --version 5.51.6 -f argocd.yaml 
k apply -f application.yaml
```

Use `helm status my-argo-cd` to get details on accessing the UI.

```
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
kubectl port-forward service/argocd-server -n argocd 8080:443 &
argocd login --insecure localhost:8080

# We used application.yaml to deploy this instead of this command:
#argocd repo add https://github.com/Equal-Vote/argocd.git #--insecure-skip-server-verification
```

# Creating Service Principal for external-dns

https://kubernetes-sigs.github.io/external-dns/v0.14.0/tutorials/azure/#service-principal

```
EXTERNALDNS_NEW_SP_NAME="ExternalDnsServicePrincipal"
AZURE_DNS_ZONE_RESOURCE_GROUP="equalvote"
AZURE_DNS_ZONE="sandbox.star.vote"
DNS_SP=$(az ad sp create-for-rbac --name $EXTERNALDNS_NEW_SP_NAME)
EXTERNALDNS_SP_APP_ID=$(echo $DNS_SP | jq -r '.appId')
EXTERNALDNS_SP_PASSWORD=$(echo $DNS_SP | jq -r '.password')
DNS_ID=$(az network dns zone show --name $AZURE_DNS_ZONE \
 --resource-group $AZURE_DNS_ZONE_RESOURCE_GROUP --query "id" --output tsv)
az role assignment create --role "Contributor" --assignee $EXTERNALDNS_SP_APP_ID --scope $DNS_ID
cat <<-EOF > azure.json
{
  "tenantId": "$(az account show --query tenantId -o tsv)",
  "subscriptionId": "$(az account show --query id -o tsv)",
  "resourceGroup": "$AZURE_DNS_ZONE_RESOURCE_GROUP",
  "aadClientId": "$EXTERNALDNS_SP_APP_ID",
  "aadClientSecret": "$EXTERNALDNS_SP_PASSWORD"
}
EOF
```
