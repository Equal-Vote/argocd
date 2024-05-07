#!/bin/bash

IDENTITY_NAME="argocd"
RESOURCE_GROUP="equalvote"
KEYVAULT_NAME="equalvote-argocd"
CLUSTER_NAME="equalvote"
SERVICE_ACCOUNT_ISSUER="$(az aks show --resource-group "${RESOURCE_GROUP}" --name "${CLUSTER_NAME}" --query 'oidcIssuerProfile.issuerUrl' -otsv)"

az identity create --name "${IDENTITY_NAME}" --resource-group "${RESOURCE_GROUP}"

IDENTITY_CLIENT_ID="$(az identity show --name ${IDENTITY_NAME} --resource-group ${RESOURCE_GROUP} --query 'clientId' -otsv)"
IDENTITY_OBJECT_ID="$(az identity show --name ${IDENTITY_NAME} --resource-group ${RESOURCE_GROUP} --query 'principalId' -otsv)"
IDENTITY_TENANT_ID="$(az identity show --name ${IDENTITY_NAME} --resource-group ${RESOURCE_GROUP} --query 'tenantId' -otsv)"

az keyvault set-policy --name $KEYVAULT_NAME \
  --key-permissions all \
  --object-id "${IDENTITY_OBJECT_ID}"

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  annotations:
    azure.workload.identity/client-id: ${IDENTITY_CLIENT_ID}
    azure.workload.identity/tenant-id: ${IDENTITY_TENANT_ID}
  labels:
     azure.workload.identity/use: "true"
  name: aks-argocd
  namespace: argocd
EOF

az identity federated-credential create \
  --name "kubernetes-federated-credential" \
  --identity-name "${IDENTITY_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --issuer "${SERVICE_ACCOUNT_ISSUER}" \
  --subject "system:serviceaccount:argocd:aks-argocd"
