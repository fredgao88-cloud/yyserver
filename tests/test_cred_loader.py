# -*- coding: utf-8 -*-
"""
TEST-CRED-001 离线自检：load_test_credentials 标准 dotenv 解析
验收口径：
1. 覆盖带引号口令 / 无引号口令 / 混合来源 / 环境变量按字段覆盖；
2. 自定义工号（TEST_OPERATOR_ID / TEST_ADMIN_ID）与口令同字段生效；
3. 模拟登录接收端断言两个工号分别配对预期口令；
4. 全程不打印、不记录任何口令明文（本文件内仅使用一次性伪造口令）。
运行：python tests/test_cred_loader.py
说明：被测函数经 AST 从 tests/test_api_integration.py 中提取执行，
      绝不触发该文件的联网集成流程。
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "test_api_integration.py")

# 一次性伪造口令，仅用于本自检，绝非真实凭据
FAKE_OP_QUOTED = "FakeOp#Quoted!1"
FAKE_ADMIN_PLAIN = "FakeAdmPlain!2"
FAKE_ENV_OP = "FakeEnvOp!3"
FAKE_ENV_ADMIN = "FakeEnvAdm!4"

KEYS = ("TEST_OPERATOR_ID", "TEST_OPERATOR_PASS", "TEST_ADMIN_ID", "TEST_ADMIN_PASS")

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def _clear_env():
    for k in KEYS:
        os.environ.pop(k, None)


def _load_cred_func():
    """从集成测试文件中按 AST 提取 load_test_credentials 单独执行，避免触发联网流程"""
    with open(MODULE_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "load_test_credentials":
            code = compile(ast.Module(body=[node], type_ignores=[]), MODULE_PATH, "exec")
            ns = {"os": os}
            exec(code, ns)
            return ns["load_test_credentials"]
    raise RuntimeError("test_api_integration.py 中未找到 load_test_credentials")


class MockLoginReceiver:
    """模拟登录接收端：仅在内存记录工号-口令配对用于断言，绝不打印、绝不落盘"""
    def __init__(self):
        self.pairs = []

    def post(self, operator_id, password):
        self.pairs.append((operator_id, password))
        return {"access_token": "mock-token"}


def _simulate_logins(cred):
    """按集成测试的登录调用口径，把两个账号的工号与口令送交模拟接收端"""
    receiver = MockLoginReceiver()
    receiver.post(cred["TEST_OPERATOR_ID"], cred["TEST_OPERATOR_PASS"])
    receiver.post(cred["TEST_ADMIN_ID"], cred["TEST_ADMIN_PASS"])
    return receiver.pairs


def main():
    load = _load_cred_func()

    # 用例 1：.env 带引号口令 + 无引号口令 + 自定义工号（无环境变量）
    with tempfile.TemporaryDirectory() as td:
        _clear_env()
        cwd = os.getcwd()
        os.chdir(td)
        try:
            with open(".env", "w", encoding="utf-8") as f:
                f.write("TEST_OPERATOR_ID=op_custom01\n")
                f.write('TEST_OPERATOR_PASS="' + FAKE_OP_QUOTED + '"\n')
                f.write("TEST_ADMIN_ID=admin_custom\n")
                f.write("TEST_ADMIN_PASS=" + FAKE_ADMIN_PLAIN + "\n")
            cred = load()
            check("1a 自定义工号生效", cred["TEST_OPERATOR_ID"] == "op_custom01" and cred["TEST_ADMIN_ID"] == "admin_custom")
            check("1b 带引号口令剥离引号", cred["TEST_OPERATOR_PASS"] == FAKE_OP_QUOTED)
            check("1c 无引号口令原样读取", cred["TEST_ADMIN_PASS"] == FAKE_ADMIN_PLAIN)
            check("1d 模拟登录工号口令配对",
                  _simulate_logins(cred) == [("op_custom01", FAKE_OP_QUOTED), ("admin_custom", FAKE_ADMIN_PLAIN)])
        finally:
            os.chdir(cwd)

    # 用例 2：环境变量按字段覆盖 .env
    with tempfile.TemporaryDirectory() as td:
        _clear_env()
        cwd = os.getcwd()
        os.chdir(td)
        try:
            with open(".env", "w", encoding="utf-8") as f:
                f.write("TEST_OPERATOR_ID=op_file\n")
                f.write("TEST_OPERATOR_PASS=" + FAKE_OP_QUOTED + "\n")
                f.write("TEST_ADMIN_ID=admin_file\n")
                f.write("TEST_ADMIN_PASS=" + FAKE_ADMIN_PLAIN + "\n")
            os.environ["TEST_OPERATOR_PASS"] = FAKE_ENV_OP
            os.environ["TEST_ADMIN_ID"] = "admin_env"
            cred = load()
            check("2a 环境变量口令覆盖 .env", cred["TEST_OPERATOR_PASS"] == FAKE_ENV_OP)
            check("2b 环境变量工号覆盖 .env", cred["TEST_ADMIN_ID"] == "admin_env")
            check("2c 未覆盖字段仍取 .env", cred["TEST_OPERATOR_ID"] == "op_file" and cred["TEST_ADMIN_PASS"] == FAKE_ADMIN_PLAIN)
            check("2d 混合来源工号口令配对",
                  _simulate_logins(cred) == [("op_file", FAKE_ENV_OP), ("admin_env", FAKE_ADMIN_PLAIN)])
        finally:
            os.chdir(cwd)
            _clear_env()

    # 用例 3：单引号口令 + 缺省工号回退 + 纯环境变量口令
    with tempfile.TemporaryDirectory() as td:
        _clear_env()
        cwd = os.getcwd()
        os.chdir(td)
        try:
            with open(".env", "w", encoding="utf-8") as f:
                f.write("TEST_OPERATOR_PASS='" + FAKE_OP_QUOTED + "'\n")
            os.environ["TEST_ADMIN_PASS"] = FAKE_ENV_ADMIN
            cred = load()
            check("3a 单引号口令剥离引号", cred["TEST_OPERATOR_PASS"] == FAKE_OP_QUOTED)
            check("3b 缺省工号回退默认", cred["TEST_OPERATOR_ID"] == "doctor01" and cred["TEST_ADMIN_ID"] == "admin")
            check("3c 纯环境变量口令生效", cred["TEST_ADMIN_PASS"] == FAKE_ENV_ADMIN)
            check("3d 混合配对",
                  _simulate_logins(cred) == [("doctor01", FAKE_OP_QUOTED), ("admin", FAKE_ENV_ADMIN)])
        finally:
            os.chdir(cwd)
            _clear_env()

    failed = [n for n, c in _results if not c]
    print(f"\n===== TEST-CRED-001 自检：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    if failed:
        print("失败项：")
        for n in failed:
            print(" -", n)
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
