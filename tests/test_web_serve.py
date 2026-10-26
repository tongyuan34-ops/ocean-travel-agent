"""前端服务器的局域网地址选择。

`web/serve.py` 会打印「手机同网访问：http://<ip>:3001」，这个地址必须是
手机真的能连上的那个。原来的实现只做了一次 `connect(("8.8.8.8", 80))` 取出口地址，
在装了代理 / VPN 的机器上会拿到代理虚拟网卡（198.18.0.0/15），
打印出来完全正常、手机却连不上——这种「不报错的错」最难排查。

所以把选择逻辑抽成纯函数，用候选列表单测，不依赖本机网卡。
"""

from __future__ import annotations

import sys
from pathlib import Path

# web/ 不是包，按脚本所在目录导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from serve import pick_lan_ip


def test_prefers_private_network():
    assert pick_lan_ip(["198.18.0.1", "192.168.31.194"]) == "192.168.31.194"


def test_skips_proxy_virtual_adapter():
    """只剩代理虚拟网卡时不能拿它糊弄，返回回环让调用方去提示。"""
    assert pick_lan_ip(["198.18.0.1"]) == "127.0.0.1"


def test_accepts_all_rfc1918_ranges():
    assert pick_lan_ip(["10.1.2.3"]) == "10.1.2.3"
    assert pick_lan_ip(["172.20.5.6"]) == "172.20.5.6"
    assert pick_lan_ip(["192.168.0.9"]) == "192.168.0.9"


def test_override_wins_when_probe_fails():
    """探测不到时用 LAN_IP 指定——这是代理环境下唯一可靠的出路。"""
    assert pick_lan_ip(["198.18.0.1"], override="192.168.31.194") == "192.168.31.194"


def test_private_network_beats_override():
    """探测已经拿到可信私有地址时，不该被一个手滑写错的环境变量带偏。"""
    assert pick_lan_ip(["192.168.31.194"], override="10.9.9.9") == "192.168.31.194"


def test_ignores_loopback_and_link_local():
    assert pick_lan_ip(["127.0.0.1", "169.254.3.4"]) == "127.0.0.1"


def test_ignores_garbage():
    assert pick_lan_ip(["", "not-an-ip", "999.1.1.1"]) == "127.0.0.1"


def test_empty_candidates():
    assert pick_lan_ip([]) == "127.0.0.1"
