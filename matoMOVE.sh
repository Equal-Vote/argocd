#!/bin/bash

# Was originally for moving matomo because it was being such a poop, but then
# we made it work with any application, but the name was too cool.

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 app_name disable|enable"
  exit 1
fi

# https://stackoverflow.com/questions/59895/how-do-i-get-the-directory-where-a-bash-script-is-located-from-within-the-script
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

export app="${1}"
export action="${2}"

if [[ "${action}" == "disable" ]]; then
  mkdir -p ${SCRIPT_DIR}/applications-disabled/
  mv "${SCRIPT_DIR}/applications/${app}" ${SCRIPT_DIR}/applications-disabled/
  git add "${SCRIPT_DIR}/applications-disabled/${app}/"
  git commit -m "Disabling ${app}"
  git push
  kubectl delete ns "${app}"
else
  mv "${SCRIPT_DIR}/applications-disabled/${app}" ${SCRIPT_DIR}/applications/
  git add "${SCRIPT_DIR}/applications/${app}/"
  git commit -m "Enabling ${app}"
  git push
fi 
