"""conduit 服务质料的整体故事：真实骨架 + 合成 docker。

需要 spine 检出（环境变量 SPINE_REPO，或同级目录 ../spine）。
真实 compose 客户端由 PATH 前置的合成 docker 顶替：只替换执行底座，
装配（argv、环境、信号、端点、换代）全走正式路径。
"""
import importlib.util
import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

MATERIAL = Path(__file__).resolve().parents[1]  # 本仓的 spine/ 质料目录
SPINE = Path(os.environ.get("SPINE_REPO",
                            MATERIAL.parent.parent / "spine")).resolve()


def load_rig():
    if not (SPINE / "tests" / "rig.py").is_file():
        raise AssertionError("spine not found at %s (set SPINE_REPO)" % SPINE)
    spec = importlib.util.spec_from_file_location("spine_rig", SPINE / "tests" / "rig.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConduitStoryTest(unittest.TestCase):
    def setUp(self):
        rig_module = load_rig()
        base = Path(tempfile.mkdtemp(prefix="spine-conduit-"))
        # 合成 docker 顶替真实 compose 客户端：整个骨架现场共用这个 PATH
        stub_bin = base / "bin"
        stub_bin.mkdir()
        shutil.copy(MATERIAL / "tests" / "fixtures" / "docker", stub_bin / "docker")
        (stub_bin / "docker").chmod(0o755)
        self.saved_path = os.environ["PATH"]
        os.environ["PATH"] = "%s%s%s" % (stub_bin, os.pathsep, self.saved_path)
        self.rig = rig_module.Rig(base)
        self.base = base

        def cleanup():
            stragglers = self.rig.close()
            os.environ["PATH"] = self.saved_path
            shutil.rmtree(base, ignore_errors=True)
            self.assertEqual(stragglers, [], "processes leaked past close")

        self.addCleanup(cleanup)

    def stage(self):
        """publish 只打包质料三件，不携测试与字节码。"""
        stage = self.base / "stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        for name in ("material.json", "serve.py", "compose.yaml"):
            shutil.copy(MATERIAL / name, stage / name)
        return stage

    def publish(self, *extra):
        port = self.port
        self.rig.spine_ok(
            "publish", str(self.stage()), "--node", "host=rig",
            "--program-arg", "serve=--url",
            "--program-arg", "serve=http://127.0.0.1:%d" % port,
            "--program-arg", "serve=--port", "--program-arg", "serve=%d" % port,
            *extra)

    def record(self):
        return (self.rig.statuses()["rig"]["materials"].get("conduit", {})
                .get("programs", {}).get("serve") or {})

    def test_conduit_story(self):
        rig = self.rig
        self.port = free_port()
        self.publish()
        state = self.base / "rig" / "state" / "conduit"
        # 服务=常驻程序：wrapper exec 成 compose 客户端，按声明的形态起动
        rig.wait(lambda: self.record().get("state") == "running",
                 message="serve not running")
        stub = rig.wait(
            lambda: json.loads((state / "stub-docker.json").read_text())
            if (state / "stub-docker.json").exists() else None,
            message="compose not invoked")
        self.assertEqual(stub["argv"][0], "compose")
        self.assertIn("up", stub["argv"])
        self.assertEqual(stub["image_tag"], "release")
        # 生产纪律进 argv：禁构建（防吞拉取错误回退本机构建）、缺镜像才拉
        self.assertIn("--no-build", stub["argv"])
        self.assertIn("missing", stub["argv"])
        # 停止期限须落在看护壳的优雅窗口（TERM 5s）之内
        compose = (MATERIAL / "compose.yaml").read_text()
        self.assertIn("stop_grace_period: 3s", compose)
        # 自述地址经心跳转发，成为驾驶舱卡片；服务对外可达
        rig.wait(lambda: (rig.statuses()["rig"]["materials"]["conduit"]["endpoints"]
                          or {}).get("service") == "http://127.0.0.1:%d" % self.port,
                 message="endpoint not forwarded")
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port,
                                    timeout=5) as response:
            self.assertEqual(response.status, 200)
        # 升级=换代：改装配参数，停旧 compose（SIGTERM 优雅）起新
        self.publish("--program-arg", "serve=--image-tag", "--program-arg", "serve=v2")
        rig.wait(lambda: (state / "stub-docker.json").exists()
                 and json.loads((state / "stub-docker.json").read_text())
                 .get("image_tag") == "v2"
                 and self.record().get("state") == "running",
                 message="upgrade did not restart service")
        # withdraw：服务停、现场回收、端口关闭
        rig.spine_ok("withdraw", "conduit")
        rig.wait(lambda: "conduit" not in rig.statuses()["rig"].get("materials", {}),
                 message="withdraw did not recycle")
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen("http://127.0.0.1:%d/" % self.port, timeout=2)


if __name__ == "__main__":
    unittest.main()
