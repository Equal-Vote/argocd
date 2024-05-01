#!/bin/bash

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 app_name disable|enable"
  exit 1
fi

export app="${1}"
export action="${2}"

if [[ "${action}" == "disable" ]]; then
  mkdir -p /home/evans/github.com/Equal-Vote/argocd/applications-disabled/
  mv "/home/evans/github.com/Equal-Vote/argocd/applications/${app}" /home/evans/github.com/Equal-Vote/argocd/applications-disabled/
  kubectl delete ns matomo
  git add "/home/evans/github.com/Equal-Vote/argocd/applications-disabled/${app}/"
  git commit -m "Disabling ${app}"
else
  mv "/home/evans/github.com/Equal-Vote/argocd/applications-disabled/${app}" /home/evans/github.com/Equal-Vote/argocd/applications/
fi 
