# keycloak-operator

Installed straight from upstream's Kustomize base,
`keycloak/keycloak-k8s-resources` at `kubernetes/`, pinned by `revision` in
`config.json`. There is no official Helm chart.

- The operator runs the Keycloak version that matches its own tag, so bumping
  `revision` upgrades Keycloak too. Read the upgrade notes first:
  https://www.keycloak.org/docs/latest/upgrading/
- It must stay in the `keycloak` namespace. Upstream's ClusterRoleBinding
  hard-codes that namespace for its ServiceAccount subject, and the operator
  watches only the namespace it runs in.
