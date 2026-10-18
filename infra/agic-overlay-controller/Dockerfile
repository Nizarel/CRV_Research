ARG AGIC_VERSION=1.9.12

FROM --platform=$BUILDPLATFORM golang:1.25-bookworm AS build
ARG AGIC_VERSION
ARG TARGETOS
ARG TARGETARCH

WORKDIR /src

RUN git clone --depth 1 --branch ${AGIC_VERSION} https://github.com/Azure/application-gateway-kubernetes-ingress.git .

COPY cni.go pkg/cni/cni.go

RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -o /out/appgw-ingress ./cmd/appgw-ingress

FROM mcr.microsoft.com/azure-application-gateway/kubernetes-ingress:1.9.12

COPY --from=build /out/appgw-ingress /appgw-ingress

CMD ["/appgw-ingress"]