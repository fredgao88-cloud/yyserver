#!/bin/sh
set -eu

WG_DIR="/etc/wireguard"
CLIENTS_DIR="${WG_DIR}/clients"
ENDPOINT_HOST="${WG_ENDPOINT_HOST:-8b000a4b23ee.sn.mynetname.net}"
ENDPOINT_PORT="${WG_ENDPOINT_PORT:-51820}"
WORKBENCH_TARGET_IP="${WG_WORKBENCH_IP:-192.168.1.100}"

mkdir -p "${WG_DIR}" "${CLIENTS_DIR}"
chmod 700 "${WG_DIR}" "${CLIENTS_DIR}"

if [ ! -f "${WG_DIR}/wg0.conf" ]; then
  umask 077
  SERVER_PRIV="$(wg genkey)"
  SERVER_PUB="$(printf '%s' "${SERVER_PRIV}" | wg pubkey)"
  printf '%s\n' "${SERVER_PRIV}" > "${WG_DIR}/server.key"
  printf '%s\n' "${SERVER_PUB}" > "${WG_DIR}/server.pub"

  cat > "${WG_DIR}/wg0.conf" <<EOF
[Interface]
PrivateKey = ${SERVER_PRIV}
ListenPort = 51820
EOF

  for idx_name in "2:peer1_mobile" "3:peer2_pc" "4:peer3_backup"; do
    host_octet="${idx_name%%:*}"
    peer_name="${idx_name#*:}"
    c_priv="$(wg genkey)"
    c_pub="$(printf '%s' "${c_priv}" | wg pubkey)"
    c_psk="$(wg genpsk)"
    c_ip="10.66.0.${host_octet}"

    cat >> "${WG_DIR}/wg0.conf" <<EOF

[Peer]
# ${peer_name}
PublicKey = ${c_pub}
PresharedKey = ${c_psk}
AllowedIPs = ${c_ip}/32
EOF

    cat > "${CLIENTS_DIR}/${peer_name}.conf" <<EOF
[Interface]
PrivateKey = ${c_priv}
Address = ${c_ip}/32

[Peer]
PublicKey = ${SERVER_PUB}
PresharedKey = ${c_psk}
Endpoint = ${ENDPOINT_HOST}:${ENDPOINT_PORT}
AllowedIPs = ${WORKBENCH_TARGET_IP}/32
PersistentKeepalive = 25
EOF
    chmod 600 "${CLIENTS_DIR}/${peer_name}.conf"
  done
  chmod 600 "${WG_DIR}/wg0.conf"
fi

ip link del dev wg0 2>/dev/null || true
ip link add dev wg0 type wireguard
ip address add 10.66.0.1/24 dev wg0
wg setconf wg0 "${WG_DIR}/wg0.conf"
ip link set mtu 1420 up dev wg0

NGINX_IP=""
for _i in 1 2 3 4 5 6 7 8 9 10; do
  NGINX_IP="$(getent hosts nginx | awk '{print $1}' | head -n1 || true)"
  if [ -n "${NGINX_IP}" ]; then
    break
  fi
  sleep 1
done
if [ -z "${NGINX_IP}" ]; then
  echo "ERROR: cannot resolve nginx on hospital_edge" >&2
  exit 1
fi

# 仅允许 WireGuard 流量访问院内 Nginx (80/443)，严格阻止访问 HIS 或其他网段
iptables -F FORWARD
iptables -P FORWARD DROP
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -i wg0 -o eth0 -d "${NGINX_IP}" -p tcp -m multiport --dports 80,443 -j ACCEPT

iptables -t nat -F PREROUTING
iptables -t nat -F POSTROUTING
iptables -t nat -A PREROUTING -i wg0 -d "${WORKBENCH_TARGET_IP}" -p tcp -m multiport --dports 80,443 -j DNAT --to-destination "${NGINX_IP}"
iptables -t nat -A POSTROUTING -o eth0 -d "${NGINX_IP}" -p tcp -m multiport --dports 80,443 -j MASQUERADE

echo "WireGuard wg0 ready -> forwarding ${WORKBENCH_TARGET_IP}:80,443 to nginx (${NGINX_IP})"

cleanup() {
  ip link del dev wg0 2>/dev/null || true
  exit 0
}
trap cleanup TERM INT

while true; do
  sleep 60 &
  wait $!
done
