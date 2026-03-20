#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-gps-mcp-server}"
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
    echo "  image     Build and push container image to registry"
    echo "  apply     Apply Kubernetes manifests"
    echo "  status    Show deployment status"
    echo "  logs      Tail MCP server logs"
    echo "  all       image + build + apply"
    echo ""
    echo "Environment:"
    echo "  NAMESPACE         Kubernetes namespace (default: gps-mcp-server)"
    echo "  OVERLAY           Kustomize overlay (default: base, option: openshift)"
    echo "  IMAGE_REGISTRY    Override container registry host"
    echo "  IMAGE             Override full image reference for apply"
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

cmd_image() {
    echo "=== Building and pushing container image ==="

    if [[ -n "${IMAGE_REGISTRY:-}" ]]; then
        REGISTRY="$IMAGE_REGISTRY"
    else
        REGISTRY=$($KUBECTL get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')
    fi

    echo "Registry: $REGISTRY"
    echo "Namespace: $NAMESPACE"

    echo "=== Logging into registry ==="
    docker login "$REGISTRY" -u "$($KUBECTL whoami)" -p "$($KUBECTL whoami -t)"

    local IMAGE_REF="$REGISTRY/$NAMESPACE/gps-mcp-server:latest"
    echo "=== Building $IMAGE_REF ==="
    docker build --provenance=false --platform=linux/amd64 -f "$REPO_ROOT/Containerfile" -t "$IMAGE_REF" "$REPO_ROOT"

    echo "=== Pushing $IMAGE_REF ==="
    docker push "$IMAGE_REF"

    echo "=== Image pushed successfully ==="
}

cmd_build() {
    echo "=== Building GPS database ==="
    uv run "$REPO_ROOT/scripts/build_db.py" --force
    "$REPO_ROOT/scripts/test.sh"
}

cmd_apply() {
    echo "=== Ensuring namespace $NAMESPACE exists ==="
    if ! $KUBECTL get namespace "$NAMESPACE" &>/dev/null; then
        if [[ "$KUBECTL" == "oc" ]]; then
            oc new-project "$NAMESPACE" --display-name="GPS"
        else
            kubectl create namespace "$NAMESPACE"
        fi
    fi

    echo "=== Applying manifests to $NAMESPACE (overlay: $OVERLAY) ==="

    # Set image override if specified or defaulting for OpenShift
    local EFFECTIVE_IMAGE="${IMAGE:-}"
    if [[ -z "$EFFECTIVE_IMAGE" && "$OVERLAY" == "openshift" ]]; then
        EFFECTIVE_IMAGE="image-registry.openshift-image-registry.svc:5000/$NAMESPACE/gps-mcp-server:latest"
    fi

    if [[ -n "$EFFECTIVE_IMAGE" ]]; then
        echo "=== Setting image to $EFFECTIVE_IMAGE ==="
        cp "$KUSTOMIZE_DIR/kustomization.yaml" "$KUSTOMIZE_DIR/kustomization.yaml.bak"
        (cd "$KUSTOMIZE_DIR" && kustomize edit set image "python:3.11-slim=$EFFECTIVE_IMAGE")
    fi

    $KUBECTL apply -k "$KUSTOMIZE_DIR"

    # Restore original kustomization.yaml
    if [[ -n "$EFFECTIVE_IMAGE" ]]; then
        mv "$KUSTOMIZE_DIR/kustomization.yaml.bak" "$KUSTOMIZE_DIR/kustomization.yaml"
    fi

    # Inject route hostname into ALLOWED_HTTP_HOSTS for DNS rebinding protection
    if [[ "$OVERLAY" == "openshift" ]]; then
        local ROUTE_HOST
        ROUTE_HOST=$($KUBECTL -n "$NAMESPACE" get route gps-mcp-server -o jsonpath='{.spec.host}' 2>/dev/null || true)
        if [[ -n "$ROUTE_HOST" ]]; then
            echo "=== Setting ALLOWED_HTTP_HOSTS=$ROUTE_HOST ==="
            $KUBECTL -n "$NAMESPACE" set env deployment/gps-mcp-server "ALLOWED_HTTP_HOSTS=$ROUTE_HOST"
        fi
    fi
    echo ""
    echo "=== Waiting for rollout ==="
    $KUBECTL -n "$NAMESPACE" rollout status deployment/gps-mcp-server --timeout=120s
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
    $KUBECTL -n "$NAMESPACE" logs -f deployment/gps-mcp-server
}

case "${1:-}" in
    build)  cmd_build ;;
    image)  cmd_image ;;
    apply)  cmd_apply ;;
    status) cmd_status ;;
    logs)   cmd_logs ;;
    all)    cmd_image && cmd_build && cmd_apply ;;
    *)      usage; exit 1 ;;
esac
