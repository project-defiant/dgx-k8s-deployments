ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE} AS builder
USER 0:0
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates && rm -rf /var/lib/apt/lists/*
COPY go.tar.gz source.tar.gz /inputs/
ARG GO_SHA256
ARG SOURCE_SHA256
RUN echo "$GO_SHA256  /inputs/go.tar.gz" | sha256sum -c - && echo "$SOURCE_SHA256  /inputs/source.tar.gz" | sha256sum -c - \
    && tar -xzf /inputs/go.tar.gz -C /usr/local && mkdir /src && tar -xzf /inputs/source.tar.gz -C /src --strip-components=1
ENV PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local CGO_ENABLED=1 GOFLAGS=-mod=mod
WORKDIR /src
ARG X_NET_VERSION
RUN go version && go mod edit -require=golang.org/x/net@v${X_NET_VERSION} && go mod download
RUN go build -trimpath -ldflags="-s -w" -o /out/nvidia-device-plugin ./cmd/nvidia-device-plugin
FROM ${RUNTIME_IMAGE} AS runtime
USER 0:0
COPY --from=builder /out/nvidia-device-plugin /usr/local/bin/nvidia-device-plugin
# Safe image default; the reviewed DaemonSet explicitly uses UID0 only for its kubelet socket.
USER 1000:1000
ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
ENTRYPOINT ["/usr/local/bin/nvidia-device-plugin"]
