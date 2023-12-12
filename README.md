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
