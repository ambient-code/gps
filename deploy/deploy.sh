#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-gps}"
OVERLAY="${OVERLAY:-base}"

# Detect kubectl or oc
if command -v oc &>/dev/null; then
    KUBECTL=oc
else
    KUBECTL=kubectl
fi

usage() {
    echo "Usage: deploy.sh <command> [--overlay base|openshift]"
    echo ""
    echo "Commands:"
    echo "  build     Build gps.db and run tests"
    echo "  apply     Apply Kubernetes manifests"
    echo "  status    Show deployment status"
    echo "  logs      Tail MCP server logs"
    echo "  all       build + apply"
    echo ""
    echo "Environment:"
    echo "  NAMESPACE   Kubernetes namespace (default: gps)"
    echo "  OVERLAY     Kustomize overlay (default: base, option: openshift)"
}

# Parse --overlay flag from any position
args=()
for arg in "$@"; do
    if [[ "$arg" == --overlay=* ]]; then
        OVERLAY="${arg#--overlay=}"
    elif [[ "${prev:-}" == "--overlay" ]]; then
        OVERLAY="$arg"
        prev=""
        continue
    elif [[ "$arg" == "--overlay" ]]; then
        prev="$arg"
        continue
    else
        args+=("$arg")
    fi
    prev=""
done
set -- "${args[@]+"${args[@]}"}"

KUSTOMIZE_DIR="$SCRIPT_DIR/k8s"
if [[ "$OVERLAY" == "openshift" ]]; then
    KUSTOMIZE_DIR="$SCRIPT_DIR/k8s/overlays/openshift"
else
    KUSTOMIZE_DIR="$SCRIPT_DIR/k8s/base"
fi

cmd_build() {
    echo "=== Building GPS database ==="
    uv run "$REPO_ROOT/scripts/build_db.py" --force
    "$REPO_ROOT/scripts/test.sh"
}

cmd_apply() {
    echo "=== Applying manifests to $NAMESPACE (overlay: $OVERLAY) ==="
    $KUBECTL apply -k "$KUSTOMIZE_DIR"
    echo ""
    echo "=== Waiting for rollout ==="
    $KUBECTL -n "$NAMESPACE" rollout status deployment/mcp-server --timeout=120s
    echo ""
    cmd_status
}

cmd_status() {
    echo "=== Deployment Status ==="
    $KUBECTL -n "$NAMESPACE" get pods
    echo ""
    if [[ "$OVERLAY" == "openshift" ]]; then
        echo "=== Routes ==="
        $KUBECTL -n "$NAMESPACE" get routes
    else
        echo "=== Services ==="
        $KUBECTL -n "$NAMESPACE" get services
    fi
}

cmd_logs() {
    $KUBECTL -n "$NAMESPACE" logs -f deployment/mcp-server
}

case "${1:-}" in
    build)  cmd_build ;;
    apply)  cmd_apply ;;
    status) cmd_status ;;
    logs)   cmd_logs ;;
    all)    cmd_build && cmd_apply ;;
    *)      usage; exit 1 ;;
esac
