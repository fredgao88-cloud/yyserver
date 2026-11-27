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


if __name__ == "__main__":
    print("=" * 70)
    print(">>> NET-EGRESS-001 隔离部署网络与出站策略验收 <<<")
    print("=" * 70)
    run_net_egress_isolated()
