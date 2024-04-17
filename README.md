# Bootstrapping

Based on https://artifacthub.io/packages/helm/argo/argo-cd.

Installing ArgoCD in the cluster via Helm:

```
helm repo add argo https://argoproj.github.io/argo-helm
kubectl create ns argocd
helm install argocd argo/argo-cd --namespace argocd --version 6.7.12
kubectl apply -f application.yaml
```

Use `helm status argocd` to get details on accessing the UI.

# Set up Azure Managed Workload Identity for external-dns

https://kubernetes-sigs.github.io/external-dns/v0.14.0/tutorials/azure/#managed-identity-using-workload-identity

```
AZURE_AKS_RESOURCE_GROUP="equalvote"
AZURE_AKS_CLUSTER_NAME="equalvote"
az aks update --resource-group ${AZURE_AKS_RESOURCE_GROUP} --name ${AZURE_AKS_CLUSTER_NAME} --enable-oidc-issuer --enable-workload-identity

IDENTITY_RESOURCE_GROUP=$AZURE_AKS_RESOURCE_GROUP
IDENTITY_NAME="equalvote-identity"
az identity create --resource-group "${IDENTITY_RESOURCE_GROUP}" --name "${IDENTITY_NAME}"

AZURE_DNS_ZONE_RESOURCE_GROUP="equalvote"
AZURE_DNS_ZONE="sandbox.star.vote"
IDENTITY_CLIENT_ID=$(az identity show --resource-group "${IDENTITY_RESOURCE_GROUP}" \
  --name "${IDENTITY_NAME}" --query "clientId" --output tsv)
DNS_ID=$(az network dns zone show --name "${AZURE_DNS_ZONE}" \
  --resource-group "${AZURE_DNS_ZONE_RESOURCE_GROUP}" --query "id" --output tsv)
RESOURCE_GROUP_ID=$(az group show --name "${AZURE_DNS_ZONE_RESOURCE_GROUP}" --query "id" --output tsv)
az role assignment create --role "DNS Zone Contributor" \
  --assignee "${IDENTITY_CLIENT_ID}" --scope "${DNS_ID}"
az role assignment create --role "Reader" \
  --assignee "${IDENTITY_CLIENT_ID}" --scope "${RESOURCE_GROUP_ID}"

OIDC_ISSUER_URL="$(az aks show -n equalvote -g equalvote --query "oidcIssuerProfile.issuerUrl" -otsv)"
az identity federated-credential create --name ${IDENTITY_NAME} --identity-name ${IDENTITY_NAME} --resource-group ${AZURE_AKS_RESOURCE_GROUP} --issuer "$OIDC_ISSUER_URL" --subject "system:serviceaccount:external-dns:external-dns"
```

# Set up Azure Managed Identity for cert-manager

```
export AZURE_DEFAULTS_GROUP=equalvote
export DOMAIN_NAME=sandbox.star.vote
export CLUSTER=equalvote
export EMAIL_ADDRESS=gmail@evanstucker.com
export AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)

export IDENTITY_NAME=cert-manager
az identity create --name "${IDENTITY_NAME}" -g $AZURE_DEFAULTS_GROUP
export IDENTITY_CLIENT_ID=$(az identity show --name "${IDENTITY_NAME}" --query 'clientId' -o tsv -g $AZURE_DEFAULTS_GROUP)
az role assignment create \
    --role "DNS Zone Contributor" \
    --assignee $IDENTITY_CLIENT_ID \
    --scope $(az network dns zone show --name $DOMAIN_NAME -o tsv --query id -g $AZURE_DEFAULTS_GROUP)

export SERVICE_ACCOUNT_NAME=cert-manager
export SERVICE_ACCOUNT_NAMESPACE=cert-manager
export SERVICE_ACCOUNT_ISSUER=$(az aks show --resource-group $AZURE_DEFAULTS_GROUP --name $CLUSTER --query "oidcIssuerProfile.issuerUrl" -o tsv)
az identity federated-credential create \
  --name "cert-manager" \
  --identity-name "${IDENTITY_NAME}" \
  --issuer "${SERVICE_ACCOUNT_ISSUER}" \
  --subject "system:serviceaccount:${SERVICE_ACCOUNT_NAMESPACE}:${SERVICE_ACCOUNT_NAME}"
```

Create the clusterissuer:

```
cat clusterissuer.template.txt | envsubst > clusterissuer.yaml
k apply -f clusterissuer.yaml
```
