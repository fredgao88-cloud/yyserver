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


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


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
    """验证隔离代理路径 (NET-EGRESS-001) 与钉钉代理脱敏日志 (NOTIFY-LOG-001)。"""
    import re
    import tempfile
    import time
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    nginx_conf_src = (repo_root / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    allow_script_path = repo_root / "nginx" / "docker-entrypoint.d" / "10-internal-allow.sh"
    assert allow_script_path.is_file(), f"缺少启动脚本: {allow_script_path}"

    api_container = os.environ.get("NET_TEST_API_CONTAINER", "hospital_agent_api")
    insp = _run(["docker", "inspect", api_container])
    assert insp.returncode == 0, f"找不到 API 容器 {api_container}: {insp.stderr}"
    api_info = json.loads(insp.stdout)[0]
    nets = api_info["NetworkSettings"]["Networks"]
    net_names = list(nets.keys())
    internal_net = net_names[0]
    # 推导出 edge 网络名称（与 docker-compose 同前缀的 hospital_edge）
    edge_net = internal_net.replace("hospital_internal", "hospital_edge")
    edge_insp = _run(["docker", "network", "inspect", edge_net])
    assert edge_insp.returncode == 0, f"找不到 edge 网络 {edge_net}: {edge_insp.stderr}"

    suffix = str(os.getpid())
    mock_his_name = f"net_test_his_edge_{suffix}"
    mock_notify_name = f"net_test_notify_edge_{suffix}"
    temp_proxy_name = f"net_test_proxy_{suffix}"

    synth_token = "SYNTH_TOK_NET_9F8E7D"
    synth_ts = "1700000000000"
    synth_sign = "SYNTH_SIG_AB12CD34"

    with tempfile.TemporaryDirectory(prefix="net_test_proxy_") as tmpdir:
        tmp_path = Path(tmpdir)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(mode=0o777, exist_ok=True)
        os.chmod(log_dir, 0o777)

        mock_his_script = (
            "import socket\n"
            "s = socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('0.0.0.0', 1433))\n"
            "s.listen(16)\n"
            "while True:\n"
            "    conn, _ = s.accept()\n"
            "    conn.sendall(b'HIS_PROXY_OK\\n')\n"
            "    conn.close()\n"
        )
        mock_notify_script = (
            "import json\n"
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_POST(self):\n"
            "        length = int(self.headers.get('Content-Length', 0))\n"
            "        body = self.rfile.read(length).decode('utf-8', 'ignore')\n"
            "        with open('/tmp/notify_hits.jsonl', 'a', encoding='utf-8') as f:\n"
            "            f.write(json.dumps({'path': self.path, 'body': body}) + '\\n')\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'{\"errcode\":0,\"errmsg\":\"ok\"}')\n"
            "    def log_message(self, *args):\n"
            "        pass\n"
            "HTTPServer(('0.0.0.0', 18080), H).serve_forever()\n"
        )

        try:
            for c in (mock_his_name, mock_notify_name, temp_proxy_name):
                _run(["docker", "rm", "-f", c])

            assert _run([
                "docker", "run", "-d", "--name", mock_his_name, "--network", edge_net,
                "hospital-agent-api:v1.2", "python", "-c", mock_his_script,
            ]).returncode == 0
            assert _run([
                "docker", "run", "-d", "--name", mock_notify_name, "--network", edge_net,
                "-e", f"MOCK_NOTIFY_SCRIPT={mock_notify_script}",
                "hospital-agent-api:v1.2", "sh", "-c",
                "python -c \"$MOCK_NOTIFY_SCRIPT\" & echo $! > /tmp/srv.pid; sleep 120",
            ]).returncode == 0

            mock_his_ip = json.loads(_run(["docker", "inspect", mock_his_name]).stdout)[0][
                "NetworkSettings"]["Networks"][edge_net]["IPAddress"]
            mock_notify_ip = json.loads(_run(["docker", "inspect", mock_notify_name]).stdout)[0][
                "NetworkSettings"]["Networks"][edge_net]["IPAddress"]

            # 基于生产 nginx/nginx.conf 替换上游为 edge 隔离 mock（保留真实日志格式与 error_log emerg 与 allow/deny include）
            patched_conf = nginx_conf_src
            patched_conf = re.sub(r"include\s+/etc/nginx/conf\.d/\*\.conf;", "", patched_conf)
            patched_conf = re.sub(r"192\.168\.20\.2:1433", f"{mock_his_ip}:1433", patched_conf)
            patched_conf = re.sub(r"proxy_pass\s+https://[^;]+;", f"proxy_pass http://{mock_notify_ip}:18080;", patched_conf)
            patched_conf = re.sub(r"proxy_ssl_[^;]+;", "", patched_conf)
            conf_path = tmp_path / "nginx.conf"
            conf_path.write_text(patched_conf, encoding="utf-8")

            run_proxy = _run([
                "docker", "run", "-d", "--name", temp_proxy_name,
                "--network", internal_net,
                "-v", f"{conf_path}:/etc/nginx/nginx.conf:ro",
                "-v", f"{allow_script_path}:/docker-entrypoint.d/10-internal-allow.sh:ro",
                "-v", f"{log_dir}:/var/log/nginx",
                "nginx:alpine",
            ])
            assert run_proxy.returncode == 0, f"启动临时代理失败: {run_proxy.stderr}"
            assert _run(["docker", "network", "connect", edge_net, temp_proxy_name]).returncode == 0
            time.sleep(1.5)

            proxy_nets = json.loads(_run(["docker", "inspect", temp_proxy_name]).stdout)[0]["NetworkSettings"]["Networks"]
            proxy_internal_ip = proxy_nets[internal_net]["IPAddress"]
            proxy_edge_ip = proxy_nets[edge_net]["IPAddress"]

            allow_conf = _run(["docker", "exec", temp_proxy_name, "cat", "/etc/nginx/internal_allow.conf"])
            assert allow_conf.returncode == 0 and "allow " in allow_conf.stdout and "deny all;" in allow_conf.stdout, (
                f"动态 allow 配置异常: {allow_conf.stdout} {allow_conf.stderr}"
            )

            # 1) API 容器直连 edge 上的 mock 目标必须不可达；经临时代理 1433 & 8443 可达；403 路径被拒
            api_probe = f"""
import json, socket, urllib.request, urllib.error

out = {{}}
for label, ip, port in [("direct_his", "{mock_his_ip}", 1433), ("direct_notify", "{mock_notify_ip}", 18080)]:
    try:
        with socket.create_connection((ip, port), timeout=1.5):
            out[label] = False
    except OSError:
        out[label] = True

with socket.create_connection(("{proxy_internal_ip}", 1433), timeout=3) as s:
    out["proxy_his"] = s.recv(64).decode("utf-8", "ignore").strip()

q = "access_token={synth_token}&timestamp={synth_ts}&sign={synth_sign}"
req_ok = urllib.request.Request(
    f"http://{proxy_internal_ip}:8443/robot/send?{{q}}",
    data=b'{{"msgtype":"text","text":{{"content":"test"}}}}',
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
with urllib.request.urlopen(req_ok, timeout=3) as r:
    out["notify_200"] = r.status

req_403 = urllib.request.Request(
    f"http://{proxy_internal_ip}:8443/forbidden?{{q}}",
    data=b"{{}}",
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
try:
    urllib.request.urlopen(req_403, timeout=3)
    out["notify_403"] = 200
except urllib.error.HTTPError as e:
    out["notify_403"] = e.code

print(json.dumps(out))
"""
            res1 = _run(["docker", "exec", api_container, "python", "-c", api_probe])
            assert res1.returncode == 0, f"代理路径探测失败: {res1.stderr}"
            d1 = json.loads(res1.stdout.strip())
            assert d1["direct_his"] is True and d1["direct_notify"] is True, f"api 容器竟可直连 edge 目标: {d1}"
            assert d1["proxy_his"] == "HIS_PROXY_OK", f"代理 HIS 1433 失败: {d1}"
            assert d1["notify_200"] == 200, f"代理钉钉 200 失败: {d1}"
            assert d1["notify_403"] == 403, f"非 /robot/send 路径未返回 403: {d1}"

            # 校验上游确已收到完整凭证（代理转发可用，仅日志脱敏）
            hits = _run(["docker", "exec", mock_notify_name, "cat", "/tmp/notify_hits.jsonl"])
            assert hits.returncode == 0 and synth_token in hits.stdout and synth_sign in hits.stdout, (
                f"上游未收到完整凭证: {hits.stdout} {hits.stderr}"
            )

            # 2) edge 非授权容器访问代理 8443 应被 internal_allow.conf 拒绝 (403)
            edge_probe = (
                "import urllib.request, urllib.error\n"
                f"req = urllib.request.Request('http://{proxy_edge_ip}:8443/robot/send?access_token={synth_token}&sign={synth_sign}', data=b'{{}}', method='POST')\n"
                "try:\n"
                "    urllib.request.urlopen(req, timeout=2)\n"
                "    print('200')\n"
                "except urllib.error.HTTPError as e:\n"
                "    print(e.code)\n"
            )
            edge_res = _run(["docker", "exec", mock_notify_name, "python", "-c", edge_probe])
            assert edge_res.returncode == 0 and edge_res.stdout.strip() == "403", (
                f"edge 非内网访问代理未被拒绝 403: {edge_res.stdout} {edge_res.stderr}"
            )

            # 3) 停掉 mock 上游监听进程（保留容器 IP 使其立即返回 TCP RST -> connect() failed 502），验证 error_log 不记录 query token/sign
            kill_srv = _run(["docker", "exec", mock_notify_name, "sh", "-c", "kill $(cat /tmp/srv.pid)"])
            assert kill_srv.returncode == 0, f"停止 mock notify 进程失败: {kill_srv.stderr}"
            time.sleep(0.3)
            err_probe = (
                "import urllib.request, urllib.error\n"
                f"req = urllib.request.Request('http://{proxy_internal_ip}:8443/robot/send?access_token={synth_token}&timestamp={synth_ts}&sign={synth_sign}', data=b'{{}}', method='POST')\n"
                "try:\n"
                "    urllib.request.urlopen(req, timeout=10)\n"
                "    print('200')\n"
                "except urllib.error.HTTPError as e:\n"
                "    print(e.code)\n"
            )
            err_res = _run(["docker", "exec", api_container, "python", "-c", err_probe])
            assert err_res.returncode == 0 and err_res.stdout.strip() in ("502", "504"), (
                f"上游宕机未返回 502/504: {err_res.stdout} {err_res.stderr}"
            )

            # 4) 停止代理本身，验证 api 访问代理端口立即失败（NET-EGRESS-001 代理强依赖负向测试）
            proxy_logs_out = _run(["docker", "logs", temp_proxy_name]).stdout + _run(["docker", "logs", temp_proxy_name]).stderr
            _run(["docker", "rm", "-f", temp_proxy_name])
            stop_probe = (
                "import socket\n"
                f"for port in (1433, 8443):\n"
                f"    try:\n"
                f"        with socket.create_connection(('{proxy_internal_ip}', port), timeout=1.5):\n"
                f"            raise AssertionError(f'port {{port}} still open')\n"
                f"    except OSError:\n"
                f"        pass\n"
                "print('STOPPED_OK')\n"
            )
            stop_res = _run(["docker", "exec", api_container, "python", "-c", stop_probe])
            assert stop_res.returncode == 0 and "STOPPED_OK" in stop_res.stdout, f"停代理负向测试失败: {stop_res.stderr}"

            # 5) 核查所有日志均无 synth_token / synth_sign，且 access.log 保留了状态码与无参 URI
            all_logs = [proxy_logs_out]
            for lf in log_dir.glob("*"):
                if lf.is_file():
                    all_logs.append(lf.read_text(encoding="utf-8", errors="ignore"))
            combined_logs = "\n".join(all_logs)
            assert synth_token not in combined_logs, f"Nginx 日志泄露合成 access_token: {combined_logs}"
            assert synth_sign not in combined_logs, f"Nginx 日志泄露合成 sign: {combined_logs}"
            access_txt = (log_dir / "access.log").read_text(encoding="utf-8", errors="ignore") if (log_dir / "access.log").exists() else ""
            assert "/robot/send" in access_txt and " 200 " in access_txt and " 403 " in access_txt, (
                f"access.log 未正确记录无参路径与状态码: {access_txt}"
            )
            print("PASS [NET-EGRESS 5 · Proxy Path & Stop-Proxy Negative]:", json.dumps(d1, ensure_ascii=False))
            print("PASS [NOTIFY-LOG-001 · Zero Secret Leak Across 200/403/502]: access.log sanitized, error_log clean")
        finally:
            for c in (mock_his_name, mock_notify_name, temp_proxy_name):
                _run(["docker", "rm", "-f", c])


if __name__ == "__main__":
    print("=" * 70)
    print(">>> NET-EGRESS-001 & NOTIFY-LOG-001 隔离部署网络与代理日志验收 <<<")
    print("=" * 70)
    run_net_egress_isolated()
    run_proxy_egress_and_notify_log_isolated()
