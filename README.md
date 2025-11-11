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
AZURE_DNS_ZONE="prod.equal.vote"
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
export DOMAIN_NAME=prod.equal.vote
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

# Using Loki/Grafana

Loki: This is a service for backing up logs

Grafana: This is the web end point for viewing those logs

1. **Verify Applications Directory**: Since we're in frugality mode, this service will only be enabled temporarily. Before you start using it make the loki directory is in applications. If not then it likely needs to be moved from the applications-disabled directory.

2. **Verify deployment**: Go to argocd.prod.equal.vote and verify that the loki cluster is present and green

3. **Verify grafana**: Depending on the loki values, we may not have grafana enabled. Run ``kubectl get services -n monitoring`` and verify that loki-grafana is present.

4. **Forward the service endpoint**: ``kubectl port-forward svc/loki-grafana 80:80 -n monitoring``, then it should be live at localhost:80

5. **Login**: Currently the username and password are both "admin"

6. **Enter Query Editor**: Open Hamburger -> Explore to enter queries. You can use code mode to run some of the example queries

# Grafana example queries

**Show all logs for the star-server backend**

```
{pod=~"star-server-app-.*"}
```

**Find all 500 Errors**

```
{pod=~"star-server-app-.*"} |~ "status:50.+"
```

**Trace a specific API request**

```
{pod=~"star-server-app-.*"} |~ "ctx:c09a38fc"
```

**500 Errors w/o robots.txt**

```
{pod=~"star-server-app-.*"} |~ "status:50.+" != "robots.txt"
```

