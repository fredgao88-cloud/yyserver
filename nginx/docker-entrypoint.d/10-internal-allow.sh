#!/bin/sh
set -eu

OUT_CONF="/etc/nginx/internal_allow.conf"

# 根据 api 服务实际解析的内网 IP 匹配本地网卡网段，自动适应任意 Docker 内部网段分配
API_IP=""
for _i in 1 2 3 4 5 6 7 8 9 10; do
  API_IP="$(getent hosts api 2>/dev/null | awk '{print $1}' | head -n1 || true)"
  if [ -n "${API_IP}" ]; then
    break
  fi
  sleep 0.5
done

INTERNAL_CIDR=""
if [ -n "${API_IP}" ]; then
  INTERNAL_CIDR="$(awk -v target="${API_IP}" '
    function ip2int(ip,   a) {
      split(ip, a, ".")
      return ((a[1] * 256 + a[2]) * 256 + a[3]) * 256 + a[4]
    }
    BEGIN {
      t = ip2int(target)
    }
    {
      split($1, p, "/")
      ip = p[1]; prefix = p[2] + 0
      if (prefix <= 0 || prefix > 32) next
      # 计算子网掩码并比对目标 IP
      mask = 0
      bit = 2147483648
      for (i = 0; i < prefix; i++) {
        mask += bit
        bit = int(bit / 2)
      }
      # 逐字节按位与避免 awk 整数溢出
      split(ip, ia, ".")
      split(target, ta, ".")
      net_match = 1
      rem = prefix
      for (k = 1; k <= 4; k++) {
        bits = (rem >= 8) ? 8 : (rem > 0 ? rem : 0)
        rem -= 8
        m = 256 - (2 ^ (8 - bits))
        if (int(ia[k] / (2 ^ (8 - bits))) != int(ta[k] / (2 ^ (8 - bits)))) {
          net_match = 0
          break
        }
      }
      if (net_match) {
        # 计算网络地址
        rem = prefix
        out = ""
        for (k = 1; k <= 4; k++) {
          bits = (rem >= 8) ? 8 : (rem > 0 ? rem : 0)
          rem -= 8
          step = 2 ^ (8 - bits)
          net_oct = int(ia[k] / step) * step
          out = (k == 1 ? net_oct : out "." net_oct)
        }
        print out "/" prefix
        exit
      }
    }
  ' <<EOF
$(ip -4 -o addr show scope global | awk '{print $4}')
EOF
)"
fi

if [ -n "${INTERNAL_CIDR}" ]; then
  cat > "${OUT_CONF}" <<EOF
# 自动识别的 hospital_internal 网段
allow ${INTERNAL_CIDR};
deny all;
EOF
else
  # 若 api 尚不可解析，拒绝所有来源而非放行未知网段
  cat > "${OUT_CONF}" <<EOF
deny all;
EOF
fi
