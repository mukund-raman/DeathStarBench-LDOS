#!/bin/bash
# install-istio.sh - Installs Istio and injects Envoy sidecars into the cluster

set -e

echo "Downloading Istio..."
curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.21.0 sh -

echo "Installing Istio Default Profile..."
cd istio-1.21.0
sudo cp bin/istioctl /usr/local/bin/istioctl

istioctl install --set profile=default -y

echo "Labeling default namespace for automatic sidecar injection..."
kubectl label namespace default istio-injection=enabled --overwrite

echo "Redeploying SocialNetwork pods to trigger Envoy sidecar injection..."
# Fast way to trigger a full recreation so ReplicaSets build new pods with sidecars
kubectl delete pods --all -n default

echo "Waiting for all pods to be Ready (including 2/2 sidecar containers)..."
kubectl wait --for=condition=Ready pod --all -n default --timeout=300s

echo "Istio Mesh Overlay has been successfully deployed and injected!"
