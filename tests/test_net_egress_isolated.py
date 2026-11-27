# -*- coding: utf-8 -*-
"""
NET-EGRESS-001 隔离部署出站与健康检查实测验收 (tests/test_net_egress_isolated.py)

遵循 TEST-ISO-001 隔离原则：
- 仅当显式配置 NET_TEST_ISOLATED=1 时执行容器网络与合成流量验证；否则安全跳过 (exit 0)。
- 不连接任何真实院外地址或真实院内生产 HIS，仅使用：
  1. 本机健康检查端点 (NET_TEST_HEALTH_URL，默认 https://127.0.0.1/health)；
  2. 同内部网络 (hospital-agent_hospital_internal) 上的临时模拟授权 HIS 监听容器；
  3. RFC 5737 TEST-NET 保留测试地址及不可达外部目标，验证无默认网关且出站被拒。
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid


def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def run_net_egress_isolated() -> None:
    if os.environ.get("NET_TEST_ISOLATED") != "1":
        print("[SKIP] TEST-ISO-001 (NET-EGRESS-001): 未设置 NET_TEST_ISOLATED=1，跳过容器网络实测")
        return

    api_container = (os.environ.get("NET_TEST_API_CONTAINER") or "hospital_agent_api").strip()
    internal_net = (os.environ.get("NET_TEST_INTERNAL_NETWORK") or "hospital-agent_hospital_internal").strip()
    health_url = (os.environ.get("NET_TEST_HEALTH_URL") or "https://127.0.0.1/health").strip()
    ca_file = (os.environ.get("NET_TEST_CA_FILE") or "").strip()

    # 1. 健康检查验证（经 Nginx HTTPS 入口转发至内部网络 API）
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
    elif os.environ.get("NET_TEST_INSECURE_LOCAL_TLS") == "1":
        # 仅当隔离宿主机使用自签名证书且未传入 CA 时显式允许本地回环验证
        ctx = ssl._create_unverified_context()
    else:
        ctx = ssl.create_default_context()

    with urllib.request.urlopen(health_url, context=ctx, timeout=5) as resp:
        assert resp.status == 200, f"健康检查 HTTP 状态码异常: {resp.status}"
        health_body = json.loads(resp.read().decode("utf-8"))
        assert health_body.get("status") == "ok", f"健康检查响应异常: {health_body}"
    print(f"PASS [NET-EGRESS 1 · Health]: {health_url} -> 200 {health_body}")

    # 2. 检查 API 容器仅接入 internal:true 网络且无默认网关
    insp = _run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", api_container])
    assert insp.returncode == 0, f"docker inspect 失败: {insp.stderr}"
    nets = json.loads(insp.stdout.strip())
    assert list(nets.keys()) == [internal_net], f"API 容器网络附件超出预期: {list(nets.keys())}"
    assert nets[internal_net].get("Gateway", "") == "", f"internal 网络不应存在网关: {nets[internal_net]}"

    net_insp = _run(["docker", "network", "inspect", "--format", "{{.Internal}}", internal_net])
    assert net_insp.returncode == 0 and net_insp.stdout.strip().lower() == "true", (
        f"网络 {internal_net} 必须设置 internal=true，实际: {net_insp.stdout}"
    )

    route_res = _run(["docker", "exec", api_container, "cat", "/proc/net/route"])
    assert route_res.returncode == 0
    for line in route_res.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2:
            assert cols[1] != "00000000", f"检测到意外默认路由 0.0.0.0: {line}"
    print(f"PASS [NET-EGRESS 2 · Topology]: {api_container} 仅挂载 internal=true 网络 ({internal_net})，无默认出站网关")

    # 3. 模拟授权 HIS 可达 + 未授权出站被拒（同网段临时模拟 HIS 容器）
    mock_name = "mock_his_" + uuid.uuid4().hex[:8]
    mock_port = 11433
    mock_server_code = (
        "import socket\n"
        f"srv = socket.socket()\n"
        "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"srv.bind(('0.0.0.0', {mock_port}))\n"
        "srv.listen(4)\n"
        "srv.settimeout(20)\n"
        "conn, _ = srv.accept()\n"
        "conn.sendall(b'HIS_MOCK_OK\\n')\n"
        "conn.close()\n"
        "srv.close()\n"
    )
    img_res = _run(["docker", "inspect", "--format", "{{.Config.Image}}", api_container])
    assert img_res.returncode == 0
    api_image = img_res.stdout.strip()

    try:
        start_mock = _run([
            "docker", "run", "-d", "--rm", "--name", mock_name,
            "--network", internal_net,
            api_image, "python", "-c", mock_server_code
        ])
        assert start_mock.returncode == 0, f"启动模拟授权 HIS 容器失败: {start_mock.stderr}"

        # 等待模拟监听就绪并获取其内网 IP
        mock_ip = ""
        for _ in range(20):
            ip_res = _run(["docker", "inspect", "--format", f"{{{{(index .NetworkSettings.Networks \"{internal_net}\").IPAddress}}}}", mock_name])
            if ip_res.returncode == 0 and ip_res.stdout.strip():
                mock_ip = ip_res.stdout.strip()
                break
            time.sleep(0.25)
        assert mock_ip, "未能获取模拟授权 HIS 容器内网 IP"
        time.sleep(0.5)

        probe_script = (
            "import json, socket\n"
            "results = {'authorized': [], 'unauthorized': []}\n"
            f"for host, port in [('{mock_ip}', {mock_port}), ('sqlserver', 1433)]:\n"
            "    s = socket.create_connection((host, port), timeout=3)\n"
            "    banner = s.recv(64).decode('ascii', 'ignore').strip() if port == 11433 else 'TCP_OK'\n"
            "    s.close()\n"
            "    results['authorized'].append({'target': f'{host}:{port}', 'ok': True, 'detail': banner})\n"
            "for host, port in [('192.0.2.1', 443), ('198.51.100.1', 80), ('8.8.8.8', 53), ('192.168.99.99', 1433)]:\n"
            "    try:\n"
            "        s = socket.create_connection((host, port), timeout=2)\n"
            "        s.close()\n"
            "        results['unauthorized'].append({'target': f'{host}:{port}', 'rejected': False})\n"
            "    except Exception as exc:\n"
            "        results['unauthorized'].append({'target': f'{host}:{port}', 'rejected': True, 'error': f'{type(exc).__name__}: {exc}'})\n"
            "print(json.dumps(results, ensure_ascii=False))\n"
        )
        probe_res = _run(["docker", "exec", api_container, "python", "-c", probe_script])
        assert probe_res.returncode == 0, f"容器内网络探测失败: {probe_res.stderr}"
        report = json.loads(probe_res.stdout.strip())

        for item in report["authorized"]:
            assert item["ok"] is True, f"授权目标不可达: {item}"
        assert report["authorized"][0]["detail"] == "HIS_MOCK_OK", f"模拟 HIS 握手异常: {report['authorized'][0]}"
        for item in report["unauthorized"]:
            assert item["rejected"] is True, f"未授权出站目标未被拒绝: {item}"

        print("PASS [NET-EGRESS 3 · Authorized Reachable]:", json.dumps(report["authorized"], ensure_ascii=False))
        print("PASS [NET-EGRESS 4 · Unauthorized Blocked]:", json.dumps(report["unauthorized"], ensure_ascii=False))
    finally:
        _run(["docker", "rm", "-f", mock_name])


def run_proxy_egress_and_notify_log_isolated():
    """
    NET-EGRESS-001 & NOTIFY-LOG-001 独立 Compose 栈全链路验收：
    - 受 TEST-ISO-001 保护：未显式设置 NET_TEST_ISOLATED=1 时立即跳过，不执行任何文件读取或子进程调用；
    - 独立 docker compose 项目构建 internal=true 与 edge 隔离网络（自包含生成临时 freetds.conf，不依赖 .gitignore 文件）；
    - 使用最小 TDS 7.4 模拟 HIS 与同时含 DNS:oapi.dingtalk.com 与 IP SAN 的测试证书 HTTPS 模拟钉钉服务，
      并向测试客户端与 Nginx 注入同一测试 Root CA；
    - 在同配置非隔离对照 (hospital_edge) 与隔离 api (internal=true) 上对照验证相同直连目标：对照成功、隔离受阻；
    - 先恢复上游确认健康，再单独停止 nginx 验证 HIS 与通知双双失败，最后重启 nginx 验证双双恢复；
    - 校验 HTTPS 200、403、上游宕机 502 下代理与上游正确传递签名凭据且代理 access/error 日志零泄露。
    """
    if os.environ.get("NET_TEST_ISOLATED") != "1":
        print("[SKIP] TEST-ISO-001 (NET-EGRESS-001 / NOTIFY-LOG-001 Proxy): 未设置 NET_TEST_ISOLATED=1，跳过容器代理与日志实测")
        return

    import re
    import tempfile
    from pathlib import Path
    from urllib.parse import parse_qs, urlparse

    repo_root = Path(__file__).resolve().parent.parent
    nginx_conf_src = (repo_root / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    allow_script_path = repo_root / "nginx" / "docker-entrypoint.d" / "10-internal-allow.sh"
    assert allow_script_path.is_file(), f"缺少启动脚本: {allow_script_path}"

    api_container = (os.environ.get("NET_TEST_API_CONTAINER") or "hospital_agent_api").strip()
    img_res = _run(["docker", "inspect", "--format", "{{.Config.Image}}", api_container])
    assert img_res.returncode == 0, f"找不到 API 容器镜像 {api_container}: {img_res.stderr}"
    api_image = img_res.stdout.strip()

    project_name = f"net_test_compose_{os.getpid()}"
    synth_token = "SYNTH_TOK_NET_9F8E7D"
    synth_secret = "SYNTH_SEC_AB12CD34"

    mock_his_tds_py = r'''import socket, struct, json, threading

def tds_pkt(ptype, status, payload, pkt_id=1):
    return struct.pack(">BBHHBB", ptype, status, 8 + len(payload), 0, pkt_id, 0) + payload

def read_tds(conn):
    buf = b""
    while True:
        hdr = b""
        while len(hdr) < 8:
            c = conn.recv(8 - len(hdr))
            if not c:
                return None, b""
            hdr += c
        ptype, status, length = hdr[0], hdr[1], struct.unpack(">H", hdr[2:4])[0]
        body = b""
        while len(body) < length - 8:
            c = conn.recv(length - 8 - len(body))
            if not c:
                return None, b""
            body += c
        buf += body
        if status & 1:
            return ptype, buf

def handle(conn, addr):
    try:
        pt, _ = read_tds(conn)
        if pt != 0x12:
            return
        pre = (
            struct.pack(">BHH", 0, 21, 6)
            + struct.pack(">BHH", 1, 27, 1)
            + struct.pack(">BHH", 2, 28, 1)
            + struct.pack(">BHH", 3, 29, 4)
            + b"\xFF"
            + b"\x0b\x00\x00\x00\x00\x00"
            + b"\x02"
            + b"\x00"
            + b"\x00\x00\x00\x00"
        )
        conn.sendall(tds_pkt(0x04, 0x01, pre))
        pt, _ = read_tds(conn)
        if pt != 0x10:
            return
        ps = "4096".encode("utf-16-le")
        env_ps = bytes([4, 4]) + ps + bytes([4]) + ps
        env = b"\xE3" + struct.pack("<H", len(env_ps)) + env_ps
        sname = "MOCKHIS".encode("utf-16-le")
        ack_body = bytes([0x01]) + struct.pack(">I", 0x74000004) + bytes([len("MOCKHIS")]) + sname + bytes([11, 0, 0, 0])
        ack = b"\xAD" + struct.pack("<H", len(ack_body)) + ack_body
        done = b"\xFD" + struct.pack("<HHQ", 0x0000, 0x00F5, 0)
        conn.sendall(tds_pkt(0x04, 0x01, env + ack + done))
        with open("/tmp/his_hits.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"peer": addr[0], "login": True}) + "\n")
        while True:
            pt, _ = read_tds(conn)
            if pt is None:
                break
            conn.sendall(tds_pkt(0x04, 0x01, b"\xFD" + struct.pack("<HHQ", 0, 0x00F6, 0)))
    except Exception:
        pass
    finally:
        conn.close()

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 1433))
srv.listen(16)
while True:
    c, a = srv.accept()
    threading.Thread(target=handle, args=(c, a), daemon=True).start()
'''

    mock_notify_https_py = r'''import json, ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "ignore")
        with open("/tmp/notify_hits.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "host": self.headers.get("Host", ""), "body": body}) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')
    def log_message(self, *args):
        pass

httpd = HTTPServer(("0.0.0.0", 443), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile="/tmp/certs/server.crt", keyfile="/tmp/certs/server.key")
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
'''

    with tempfile.TemporaryDirectory(prefix="net_test_compose_") as tmpdir:
        tmp_path = Path(tmpdir)
        log_dir = tmp_path / "logs"
        cert_dir = tmp_path / "certs"
        log_dir.mkdir(mode=0o777, exist_ok=True)
        cert_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(log_dir, 0o777)

        # 生成测试 Root CA（服务端叶证书将在 mock_notify 拿到 edge IP 后签发，同时包含 DNS:oapi.dingtalk.com 与 IP SAN）
        ca_key = cert_dir / "ca.key"
        ca_crt = cert_dir / "ca.crt"
        srv_key = cert_dir / "server.key"
        srv_csr = cert_dir / "server.csr"
        srv_crt = cert_dir / "server.crt"
        srv_ext = cert_dir / "server.ext"
        assert _run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "2",
            "-subj", "/CN=NetTest Root CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ]).returncode == 0, "生成测试 Root CA 失败"
        os.chmod(ca_crt, 0o644)

        freetds_path = tmp_path / "freetds.conf"
        freetds_path.write_text(
            "[global]\n"
            "    tds version = 7.4\n"
            "    openssl ciphers = DEFAULT@SECLEVEL=0\n\n"
            "[hisdb_proxy]\n"
            "    host = nginx\n"
            "    port = 1433\n"
            "    tds version = 7.4\n"
            "    encryption = off\n",
            encoding="utf-8",
        )
        os.chmod(freetds_path, 0o644)

        his_script_path = tmp_path / "mock_his_tds.py"
        his_script_path.write_text(mock_his_tds_py, encoding="utf-8")
        notify_script_path = tmp_path / "mock_notify_https.py"
        notify_script_path.write_text(mock_notify_https_py, encoding="utf-8")
        empty_conf_path = tmp_path / "empty.conf"
        empty_conf_path.write_text("", encoding="utf-8")

        # 保留生产 http 块（含 https://oapi.dingtalk.com + proxy_ssl_verify on + dingtalk_safe + error_log emerg），仅把 stream HIS 上游切到隔离 Compose mock_his:1433
        patched_conf = re.sub(r"192\.168\.20\.2:1433", "mock_his:1433", nginx_conf_src)
        conf_path = tmp_path / "nginx.conf"
        conf_path.write_text(patched_conf, encoding="utf-8")

        compose_path = tmp_path / "compose.yml"
        compose_path.write_text(
            f"""services:
  mock_his:
    image: {api_image}
    networks:
      - hospital_edge
    volumes:
      - {his_script_path}:/tmp/mock_his_tds.py:ro
    command: ["sh", "-c", "python /tmp/mock_his_tds.py & echo $$! > /tmp/his.pid; sleep 180"]

  mock_notify:
    image: {api_image}
    networks:
      hospital_edge:
        aliases:
          - oapi.dingtalk.com
    volumes:
      - {notify_script_path}:/tmp/mock_notify_https.py:ro
      - {cert_dir}:/tmp/certs:ro
    command: ["sh", "-c", "i=0; while [ ! -f /tmp/certs/server.crt ] && [ $$i -lt 100 ]; do sleep 0.2; i=$$((i+1)); done; python /tmp/mock_notify_https.py & echo $$! > /tmp/srv.pid; sleep 180"]

  api:
    image: {api_image}
    networks:
      - hospital_internal
    environment:
      - FREETDSCONF=/tmp/test_freetds.conf
      - HIS_FREETDS_SECTION=hisdb_proxy
      - HIS_LOGIN_TIMEOUT=3
      - HIS_USER=test_reader
      - HIS_PASS=SyntheticHisPass_123
      - HIS_DB=his
      - SSL_CERT_FILE=/tmp/test_ca.crt
      - DINGTALK_NOTIFY_ENABLED=true
      - DINGTALK_WEBHOOK_URL=http://nginx:8443/robot/send?access_token={synth_token}
      - DINGTALK_WEBHOOK_SECRET={synth_secret}
    volumes:
      - {repo_root}/db:/opt/hospital-agent/db:ro
      - {repo_root}/tools:/opt/hospital-agent/tools:ro
      - {freetds_path}:/tmp/test_freetds.conf:ro
      - {ca_crt}:/tmp/test_ca.crt:ro
    command: ["sleep", "180"]

  api_open:
    image: {api_image}
    networks:
      - hospital_edge
    environment:
      - FREETDSCONF=/tmp/test_freetds.conf
      - HIS_LOGIN_TIMEOUT=3
      - HIS_USER=test_reader
      - HIS_PASS=SyntheticHisPass_123
      - HIS_DB=his
      - SSL_CERT_FILE=/tmp/test_ca.crt
      - DINGTALK_NOTIFY_ENABLED=true
    volumes:
      - {repo_root}/db:/opt/hospital-agent/db:ro
      - {repo_root}/tools:/opt/hospital-agent/tools:ro
      - {freetds_path}:/tmp/test_freetds.conf:ro
      - {ca_crt}:/tmp/test_ca.crt:ro
    command: ["sleep", "180"]

  nginx:
    image: nginx:alpine
    networks:
      - hospital_internal
      - hospital_edge
    depends_on:
      - api
      - mock_his
      - mock_notify
    volumes:
      - {conf_path}:/etc/nginx/nginx.conf:ro
      - {empty_conf_path}:/etc/nginx/conf.d/default.conf:ro
      - {allow_script_path}:/docker-entrypoint.d/10-internal-allow.sh:ro
      - {ca_crt}:/etc/ssl/certs/ca-certificates.crt:ro
      - {log_dir}:/var/log/nginx

networks:
  hospital_internal:
    driver: bridge
    internal: true
  hospital_edge:
    driver: bridge
""",
            encoding="utf-8",
        )

        compose_base = ["docker", "compose", "-f", str(compose_path), "-p", project_name]
        try:
            up_res = _run(compose_base + ["up", "-d"])
            assert up_res.returncode == 0, f"启动独立 Compose 测试栈失败: {up_res.stderr}"
            time.sleep(2.0)

            c_ids = {}
            for svc in ("api", "api_open", "nginx", "mock_his", "mock_notify"):
                r = _run(compose_base + ["ps", "-q", svc])
                assert r.returncode == 0 and r.stdout.strip(), f"找不到 Compose 服务 {svc}: {r.stderr}"
                c_ids[svc] = r.stdout.strip()

            mock_his_edge_ip = json.loads(_run(["docker", "inspect", c_ids["mock_his"]]).stdout)[0][
                "NetworkSettings"]["Networks"][f"{project_name}_hospital_edge"]["IPAddress"]
            mock_notify_edge_ip = json.loads(_run(["docker", "inspect", c_ids["mock_notify"]]).stdout)[0][
                "NetworkSettings"]["Networks"][f"{project_name}_hospital_edge"]["IPAddress"]
            proxy_edge_ip = json.loads(_run(["docker", "inspect", c_ids["nginx"]]).stdout)[0][
                "NetworkSettings"]["Networks"][f"{project_name}_hospital_edge"]["IPAddress"]

            # 签发同时覆盖 DNS:oapi.dingtalk.com 与 IP:mock_notify_edge_ip 的测试服务端证书并通知 mock_notify 启动 HTTPS 监听
            srv_ext.write_text(
                "basicConstraints=CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
                f"subjectAltName=DNS:oapi.dingtalk.com,IP:{mock_notify_edge_ip}\n",
                encoding="utf-8",
            )
            assert _run([
                "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(srv_key), "-out", str(srv_csr),
                "-subj", "/CN=oapi.dingtalk.com",
            ]).returncode == 0, "生成测试服务端 CSR 失败"
            assert _run([
                "openssl", "x509", "-req", "-in", str(srv_csr),
                "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                "-out", str(srv_crt), "-days", "2", "-extfile", str(srv_ext),
            ]).returncode == 0, "签发测试服务端证书失败"
            for p in (srv_crt, srv_key):
                os.chmod(p, 0o644)

            # 等待 mock_notify 启动完成且 HTTPS 端口就绪
            for _ in range(30):
                ready_r = _run([
                    "docker", "exec", c_ids["api_open"], "python", "-c",
                    f"import socket; s=socket.create_connection(('{mock_notify_edge_ip}', 443), timeout=1); s.close()"
                ])
                if ready_r.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("mock_notify HTTPS 443 端口未及时就绪")

            allow_conf = _run(["docker", "exec", c_ids["nginx"], "cat", "/etc/nginx/internal_allow.conf"])
            assert allow_conf.returncode == 0 and "allow " in allow_conf.stdout and "deny all;" in allow_conf.stdout, (
                f"动态 allow 配置异常: {allow_conf.stdout} {allow_conf.stderr}"
            )

            # 1) 经 Compose 服务别名 nginx 调用真实应用 HISReader 与 send_dingtalk_alert (HTTPS proxy_ssl_verify on)
            app_via_proxy_script = """
import json, urllib.request, urllib.error
from db.his_reader import HISReader
from tools.dingtalk_notifier import send_dingtalk_alert

out = {}
conn = HISReader().get_connection()
cur = conn.cursor()
cur.execute("SELECT 1")
conn.close()
out["his_proxy_ok"] = True

res = send_dingtalk_alert(
    {"RED": 1, "YELLOW": 0, "GREEN": 2, "total_scanned": 3, "rule_version": "2026_SAMPLE_V1", "data_cutoff_time": "2026-11-27 10:00:00"}
)
out["notify_proxy"] = res

# 同时验证非 /robot/send 路径返回 403 且不泄露查询串
req_403 = urllib.request.Request(
    "http://nginx:8443/forbidden?access_token=SYNTH_TOK_NET_9F8E7D&sign=SYNTH_SIG_403",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req_403, timeout=3)
    out["status_403"] = 200
except urllib.error.HTTPError as e:
    out["status_403"] = e.code

print(json.dumps(out, ensure_ascii=False))
"""
            res_ok = _run(["docker", "exec", c_ids["api"], "python", "-c", app_via_proxy_script])
            assert res_ok.returncode == 0, f"经 nginx 代理的应用调用失败: {res_ok.stderr}\n{res_ok.stdout}"
            d_ok = json.loads(res_ok.stdout.strip())
            assert d_ok["his_proxy_ok"] is True
            assert d_ok["notify_proxy"].get("sent") is True and d_ok["notify_proxy"].get("response", {}).get("errcode") == 0, (
                f"send_dingtalk_alert 经 nginx 代理未成功: {d_ok}"
            )
            assert d_ok["status_403"] == 403

            # 核实 HIS 与 HTTPS 钉钉模拟上游均真实收到应用流量，且带齐 token/timestamp/sign
            his_hits = _run(["docker", "exec", c_ids["mock_his"], "cat", "/tmp/his_hits.jsonl"])
            assert his_hits.returncode == 0 and '"login": true' in his_hits.stdout, f"mock_his 未收到应用连接: {his_hits.stderr}"

            notify_hits = _run(["docker", "exec", c_ids["mock_notify"], "cat", "/tmp/notify_hits.jsonl"])
            assert notify_hits.returncode == 0 and notify_hits.stdout.strip(), f"mock_notify 未收到 HTTPS 请求: {notify_hits.stderr}"
            hit_obj = json.loads(notify_hits.stdout.strip().splitlines()[-1])
            assert hit_obj["host"] == "oapi.dingtalk.com", f"上游 Host 头不匹配: {hit_obj}"
            qs = parse_qs(urlparse(hit_obj["path"]).query)
            assert qs.get("access_token") == [synth_token] and qs.get("timestamp") and qs.get("sign"), (
                f"HTTPS 模拟上游未收到完整签名参数: {hit_obj}"
            )
            captured_sign = qs["sign"][0]

            # 2) 直连正反对照：同一目标 URL (含可信 Test CA 与匹配 IP SAN 证书) 与 [hisdb_direct]，
            #    在放通对照容器 api_open (hospital_edge) 上必须双双成功，在隔离容器 api (internal:true) 内必须双双受阻且非 TLS 报错
            direct_script = f"""
import json, os, ssl, urllib.request, urllib.error
from pathlib import Path
from db.his_reader import HISReader
from tools.dingtalk_notifier import send_dingtalk_alert

orig_conf = Path("/tmp/test_freetds.conf").read_text(encoding="utf-8")
tmp_conf = Path("/tmp/freetds_direct.conf")
tmp_conf.write_text(
    orig_conf + "\\n[hisdb_direct]\\n    host = {mock_his_edge_ip}\\n    port = 1433\\n    tds version = 7.4\\n    encryption = off\\n",
    encoding="utf-8",
)
os.environ["FREETDSCONF"] = str(tmp_conf)
os.environ["HIS_FREETDS_SECTION"] = "hisdb_direct"

his_ok = False
his_err = ""
try:
    c = HISReader().get_connection()
    cur = c.cursor()
    cur.execute("SELECT 1")
    c.close()
    his_ok = True
except Exception as exc:
    his_err = f"{{type(exc).__name__}}: {{exc}}"

notify_res = send_dingtalk_alert(
    {{"RED": 1, "YELLOW": 0, "GREEN": 1, "total_scanned": 2, "rule_version": "2026_SAMPLE_V1", "data_cutoff_time": "2026-11-27 10:00:00"}},
    webhook_url="https://{mock_notify_edge_ip}:443/robot/send?access_token={synth_token}",
    secret="{synth_secret}",
    timeout=2,
)

# 探明底层直连 HTTPS 是否因 TLS 失败还是纯网络不可达
direct_ssl_error = False
try:
    urllib.request.urlopen("https://{mock_notify_edge_ip}:443/robot/send", timeout=2)
except urllib.error.URLError as e:
    if isinstance(e.reason, ssl.SSLError):
        direct_ssl_error = True
except Exception:
    pass

print(json.dumps({{
    "his_ok": his_ok,
    "his_err": his_err,
    "notify_res": notify_res,
    "direct_ssl_error": direct_ssl_error,
}}, ensure_ascii=False))
"""
            # 2a) 非隔离对照容器 (hospital_edge)：证明证书与 IP SAN、CA 信任、HIS 直连配置全部有效，可直连成功
            res_open = _run(["docker", "exec", c_ids["api_open"], "python", "-c", direct_script])
            assert res_open.returncode == 0, f"放通对照检测异常: {res_open.stderr}\n{res_open.stdout}"
            d_open = json.loads(res_open.stdout.strip())
            assert d_open["his_ok"] is True, f"放通网络对照组 HIS 直连应成功却失败: {d_open}"
            assert d_open["notify_res"].get("sent") is True and d_open["notify_res"].get("response", {}).get("errcode") == 0, (
                f"放通网络对照组直连 HTTPS 通知（含 Test CA 与 IP SAN）应成功却失败: {d_open}"
            )

            # 2b) 隔离应用容器 (internal:true)：同一目标、同一 CA 与配置，在隔离网络内必须因网络不可达失败（绝非 TLS 证书错误）
            res_direct = _run(["docker", "exec", c_ids["api"], "python", "-c", direct_script])
            assert res_direct.returncode == 0, f"直连阻断检查异常: {res_direct.stderr}"
            d_dir = json.loads(res_direct.stdout.strip())
            assert d_dir["his_ok"] is False, f"HISReader 竟允许在隔离网直连绕过代理: {d_dir}"
            assert d_dir["notify_res"].get("sent") is False and str(d_dir["notify_res"].get("reason", "")).startswith("NETWORK_ERROR:"), (
                f"send_dingtalk_alert 直连竟未受阻: {d_dir}"
            )
            assert d_dir["direct_ssl_error"] is False, f"直连失败原因竟为 TLS 证书报错而非网络隔离: {d_dir}"

            # edge 侧非内网容器直接访问代理 8443 应返回 403
            edge_res = _run([
                "docker", "exec", c_ids["mock_notify"], "python", "-c",
                f"import urllib.request, urllib.error\n"
                f"req = urllib.request.Request('http://{proxy_edge_ip}:8443/robot/send?access_token={synth_token}&sign=SYNTH_EDGE', data=b'{{}}', method='POST')\n"
                f"try:\n  urllib.request.urlopen(req, timeout=2); print('200')\n"
                f"except urllib.error.HTTPError as e:\n  print(e.code)"
            ])
            assert edge_res.returncode == 0 and edge_res.stdout.strip() == "403", f"edge 侧访问代理未被拒 403: {edge_res.stdout} {edge_res.stderr}"

            # 3) 停掉 HTTPS 模拟上游（触发 connect() failed -> 502），验证失败后立即恢复上游并确认真实通知恢复成功
            assert _run(["docker", "exec", c_ids["mock_notify"], "sh", "-c", "kill $(cat /tmp/srv.pid)"]).returncode == 0
            time.sleep(0.3)
            res_502 = _run([
                "docker", "exec", c_ids["api"], "python", "-c",
                "import json; from tools.dingtalk_notifier import send_dingtalk_alert; "
                "print(json.dumps(send_dingtalk_alert({'RED': 1, 'YELLOW': 0, 'GREEN': 1, 'total_scanned': 2, 'rule_version': '2026_SAMPLE_V1', 'data_cutoff_time': '2026-11-27 10:00:00'})))"
            ])
            assert res_502.returncode == 0
            d_502 = json.loads(res_502.stdout.strip())
            assert d_502.get("sent") is False and str(d_502.get("reason", "")).startswith("NETWORK_ERROR:"), f"上游宕机未触发失败: {d_502}"

            # 恢复 mock_notify 上游并断言经 nginx 发送再次成功，从而把后续失败明确归因于停止 nginx 本身
            assert _run([
                "docker", "exec", "-d", c_ids["mock_notify"], "sh", "-c",
                "python /tmp/mock_notify_https.py & echo $! > /tmp/srv.pid"
            ]).returncode == 0
            for _ in range(25):
                r_up = _run([
                    "docker", "exec", c_ids["api_open"], "python", "-c",
                    f"import socket; s=socket.create_connection(('{mock_notify_edge_ip}', 443), timeout=1); s.close()"
                ])
                if r_up.returncode == 0:
                    break
                time.sleep(0.2)
            res_upstream_recovered = _run([
                "docker", "exec", c_ids["api"], "python", "-c",
                "import json; from tools.dingtalk_notifier import send_dingtalk_alert; "
                "print(json.dumps(send_dingtalk_alert({'RED': 1, 'YELLOW': 0, 'GREEN': 1, 'total_scanned': 2, 'rule_version': '2026_SAMPLE_V1', 'data_cutoff_time': '2026-11-27 10:00:00'})))"
            ])
            assert res_upstream_recovered.returncode == 0
            d_up_rec = json.loads(res_upstream_recovered.stdout.strip())
            assert d_up_rec.get("sent") is True, f"恢复上游后通知未恢复成功: {d_up_rec}"

            nginx_logs_before_stop = _run(["docker", "logs", c_ids["nginx"]])

            # 4) 在两个模拟上游均健康的前提下，单独停止 nginx，验证 HISReader 与 send_dingtalk_alert 双双失败；随后启动 nginx，验证两者双双恢复成功
            assert _run(compose_base + ["stop", "-t", "1", "nginx"]).returncode == 0
            proxy_pair_probe = """
import json
from db.his_reader import HISReader
from tools.dingtalk_notifier import send_dingtalk_alert

his_ok = False
try:
    conn = HISReader().get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    conn.close()
    his_ok = True
except Exception:
    his_ok = False

notify_res = send_dingtalk_alert(
    {"RED": 1, "YELLOW": 0, "GREEN": 1, "total_scanned": 2, "rule_version": "2026_SAMPLE_V1", "data_cutoff_time": "2026-11-27 10:00:00"},
    timeout=2,
)
print(json.dumps({"his_ok": his_ok, "notify_res": notify_res}, ensure_ascii=False))
"""
            res_stopped = _run(["docker", "exec", c_ids["api"], "python", "-c", proxy_pair_probe])
            assert res_stopped.returncode == 0, f"停代理验证执行失败: {res_stopped.stderr}"
            d_stop = json.loads(res_stopped.stdout.strip())
            assert d_stop["his_ok"] is False, f"停 nginx 后 HISReader 仍成功: {d_stop}"
            assert d_stop["notify_res"].get("sent") is False and str(d_stop["notify_res"].get("reason", "")).startswith("NETWORK_ERROR:"), (
                f"停 nginx 后 send_dingtalk_alert 未失败: {d_stop}"
            )

            assert _run(compose_base + ["start", "nginx"]).returncode == 0
            for _ in range(25):
                chk_allow = _run(["docker", "exec", c_ids["nginx"], "cat", "/etc/nginx/internal_allow.conf"])
                if chk_allow.returncode == 0 and "allow " in chk_allow.stdout:
                    break
                time.sleep(0.2)
            time.sleep(0.5)
            res_recovered = _run(["docker", "exec", c_ids["api"], "python", "-c", proxy_pair_probe])
            assert res_recovered.returncode == 0, f"重启代理后恢复验证失败: {res_recovered.stderr}"
            d_rec = json.loads(res_recovered.stdout.strip())
            assert d_rec["his_ok"] is True, f"恢复 nginx 后 HISReader 未恢复成功: {d_rec}"
            assert d_rec["notify_res"].get("sent") is True and d_rec["notify_res"].get("response", {}).get("errcode") == 0, (
                f"恢复 nginx 后 send_dingtalk_alert 未恢复成功: {d_rec}"
            )

            # 5) 核查全部 Nginx 日志与容器输出均无 token/secret/sign，且 access.log 保留 200/403/502 无参路径
            all_logs = [nginx_logs_before_stop.stdout, nginx_logs_before_stop.stderr]
            for lf in log_dir.glob("*"):
                if lf.is_file():
                    all_logs.append(lf.read_text(encoding="utf-8", errors="ignore"))
            combined_logs = "\n".join(all_logs)
            for sensitive in (synth_token, synth_secret, captured_sign, "SYNTH_SIG_403", "SYNTH_EDGE", "access_token=", "sign="):
                assert sensitive not in combined_logs, f"Nginx 日志发现敏感内容 {sensitive!r}: {combined_logs}"
            access_txt = (log_dir / "access.log").read_text(encoding="utf-8", errors="ignore") if (log_dir / "access.log").exists() else ""
            assert "/robot/send" in access_txt and " 200 " in access_txt and " 403 " in access_txt and " 502 " in access_txt, (
                f"access.log 未完整保留 200/403/502 状态与无参路径: {access_txt}"
            )

            print("PASS [NET-EGRESS 5 · Isolated Compose App Paths (Open-Net Control OK, Isolated Direct Blocked, Stop/Start Nginx Verified)]:", json.dumps({"via_proxy": d_ok, "open_control_direct": {"his_ok": d_open["his_ok"], "notify_sent": d_open["notify_res"]["sent"]}, "after_nginx_recover": {"his_ok": d_rec["his_ok"], "notify_sent": d_rec["notify_res"]["sent"]}}, ensure_ascii=False))
            print("PASS [NOTIFY-LOG-001 · Zero Secret Leak Across HTTPS 200/403/502]: access.log sanitized, error_log clean")
        finally:
            _run(compose_base + ["down", "-t", "1", "-v", "--remove-orphans"], timeout=60)


if __name__ == "__main__":
    print("=" * 70)
    print(">>> NET-EGRESS-001 & NOTIFY-LOG-001 隔离部署网络与代理日志验收 <<<")
    print("=" * 70)
    run_net_egress_isolated()
    run_proxy_egress_and_notify_log_isolated()
