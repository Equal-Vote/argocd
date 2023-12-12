# Bootstrapping

Based on https://artifacthub.io/packages/helm/argo/argo-cd.

Installing ArgoCD in the cluster via Helm:

```
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd --version 5.51.6 -f argocd.yaml 
```

Use `helm status my-argo-cd` to get details on accessing the UI.
