FROM nginx:alpine

RUN apk add --no-cache wireguard-tools iptables iproute2 libqrencode-tools

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
