"""掃描區域網路中的 IP 攝影機。

預設掃描本機所在的 /24 網段，也可用 -s 自訂網段，可重複指定多個：
    python scanner.py
    python scanner.py -s 192.168.1.0/24
    python scanner.py -s 192.168.1.10-50 -s 10.0.0.0/28
    python scanner.py --export cameras_found.yaml

偵測方式：
    1. 對每個 IP 的常見攝影機埠做 TCP 連線測試
    2. 開放 RTSP 埠者送出 RTSP OPTIONS，確認確實是 RTSP 服務
    3. 開放 HTTP 埠者讀取 Server 標頭與網頁標題，推測廠牌
    4. 發送 ONVIF WS-Discovery 群播，找出支援 ONVIF 的攝影機（僅限本機所在網段）
只用 Python 標準函式庫，不需額外安裝套件。
"""

import argparse
import ipaddress
import re
import socket
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

RTSP_PORTS = (554, 8554)
HTTP_PORTS = (80, 8080)
# 廠牌私有協定埠：有開就幾乎可以確定是該廠牌的攝影機／錄影機
VENDOR_PORTS = {8000: "Hikvision", 37777: "Dahua", 34567: "XiongMai"}
DEFAULT_PORTS = RTSP_PORTS + HTTP_PORTS + tuple(VENDOR_PORTS)

# 由 RTSP/HTTP 回應內容推測廠牌（不分大小寫比對）
VENDOR_KEYWORDS = [
    ("hikvision", "Hikvision"), ("dvrdvs-webs", "Hikvision"), ("app-webs", "Hikvision"),
    ("dnvrs-webs", "Hikvision"), ("dahua", "Dahua"), ("uc-httpd", "XiongMai"),
    ("axis", "Axis"), ("tp-link", "TP-Link"), ("tapo", "TP-Link"), ("reolink", "Reolink"),
    ("uniview", "Uniview"), ("vivotek", "Vivotek"), ("hanwha", "Hanwha"), ("wisenet", "Hanwha"),
    ("foscam", "Foscam"), ("amcrest", "Amcrest"), ("ezviz", "EZVIZ"), ("imou", "Imou"),
]

# 與 config.example.yaml 說明一致的各廠牌預設 RTSP 路徑
DEFAULT_PATHS = {
    "Hikvision": "/Streaming/Channels/101",
    "Dahua": "/cam/realmonitor?channel=1&subtype=0",
    "Amcrest": "/cam/realmonitor?channel=1&subtype=0",
}
FALLBACK_PATH = "/stream1"

MAX_HOSTS = 65536  # 避免誤打成 /8 之類的超大網段

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
WS_DISCOVERY_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msg_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def local_ip():
    """取得本機對外使用的 IPv4 位址。UDP connect 只會選路由，不會真的送出封包。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return None


def default_network():
    ip = local_ip()
    if ip is None or ip.startswith("127."):
        sys.exit("無法取得本機 IP，請用 -s 指定要掃描的網段，例如 -s 192.168.0.0/24")
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def parse_target(text):
    """解析網段字串，回傳 IP 清單。

    支援：192.168.1.0/24、192.168.1.10-50、192.168.1.10-192.168.1.50、192.168.1.7
    """
    text = text.strip()
    try:
        if "/" in text:
            net = ipaddress.ip_network(text, strict=False)
            # /31、/32 沒有網路與廣播位址之分，hosts() 才會回傳全部位址
            return list(net.hosts()) if net.num_addresses > 2 else list(net)
        if "-" in text:
            start_text, end_text = (p.strip() for p in text.split("-", 1))
            start = ipaddress.IPv4Address(start_text)
            if "." in end_text:
                end = ipaddress.IPv4Address(end_text)
            else:
                end = ipaddress.IPv4Address(start_text.rsplit(".", 1)[0] + "." + end_text)
            if end < start:
                raise ValueError("結束位址小於起始位址")
            return [ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1)]
        return [ipaddress.IPv4Address(text)]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"無法解析網段「{text}」：{e}") from None


def port_open(ip, port, timeout):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((ip, port)) == 0


def recv_head(sock, limit=8192):
    """讀到標頭結束（空行）或連線關閉為止。"""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data.decode("latin-1", errors="replace")


def header(response, name):
    m = re.search(rf"^{name}:\s*(.+?)\s*$", response, re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else ""


def probe_rtsp(ip, port, timeout):
    """送出 RTSP OPTIONS；是 RTSP 服務就回傳回應內容，否則回傳 None。"""
    request = f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: multi-ipcam-hub\r\n\r\n"
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(request.encode())
            response = recv_head(s)
    except OSError:
        return None
    return response if response.startswith("RTSP/") else None


def probe_http(ip, port, timeout):
    """讀取首頁的回應標頭與部分內容，回傳 (Server 標頭, 網頁標題, 原始回應)。"""
    request = f"GET / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: multi-ipcam-hub\r\n\r\n"
    data = b""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(request.encode())
            while len(data) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        pass  # 逾時前已收到的部分仍可拿來判斷
    text = data.decode("utf-8", errors="replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return header(text, "Server"), (title.group(1).strip() if title else ""), text


def guess_vendor(*texts):
    blob = " ".join(texts).lower()
    for keyword, vendor in VENDOR_KEYWORDS:
        if keyword in blob:
            return vendor
    return ""


def onvif_discover(bind_ip, wait):
    """WS-Discovery 群播探測，回傳 {ip: {"xaddrs": [...], "name": str, "hardware": str}}。"""
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
        s.bind((bind_ip, 0))
        s.settimeout(0.5)
        probe = WS_DISCOVERY_PROBE.format(msg_id=uuid.uuid4()).encode()
        try:
            s.sendto(probe, WS_DISCOVERY_ADDR)
        except OSError as e:
            print(f"  ONVIF 探測送出失敗：{e}", file=sys.stderr)
            return found
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, (ip, _) = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            xaddrs = re.search(r"<[^>]*XAddrs>(.*?)</", text, re.DOTALL)
            scopes = re.search(r"<[^>]*Scopes[^>]*>(.*?)</", text, re.DOTALL)
            scopes = scopes.group(1) if scopes else ""
            name = re.search(r"onvif://www\.onvif\.org/name/(\S+)", scopes)
            hardware = re.search(r"onvif://www\.onvif\.org/hardware/(\S+)", scopes)
            found[ip] = {
                "xaddrs": xaddrs.group(1).split() if xaddrs else [],
                "name": unquote(name.group(1)) if name else "",
                "hardware": unquote(hardware.group(1)) if hardware else "",
            }
    return found


def scan(ips, ports, timeout, workers, onvif_wait):
    ips = [str(ip) for ip in ips]
    results = {}  # ip -> dict

    print(f"掃描 {len(ips)} 個位址的埠 {', '.join(map(str, ports))} ...", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = [(ip, port) for ip in ips for port in ports]
        for (ip, port), is_open in zip(tasks, pool.map(lambda t: port_open(*t, timeout), tasks)):
            if is_open:
                results.setdefault(ip, {"ports": [], "rtsp": [], "server": "", "title": "",
                                        "vendor": "", "onvif": None})["ports"].append(port)

        def probe(ip):
            info = results[ip]
            texts = []
            for port in info["ports"]:
                if port in RTSP_PORTS:
                    resp = probe_rtsp(ip, port, timeout * 2)
                    if resp:
                        info["rtsp"].append(port)
                        texts.append(header(resp, "Server"))
                elif port in HTTP_PORTS and not info["server"]:
                    info["server"], info["title"], raw = probe_http(ip, port, timeout * 2)
                    texts.append(raw)
            info["vendor"] = guess_vendor(*texts) or next(
                (VENDOR_PORTS[p] for p in info["ports"] if p in VENDOR_PORTS), "")

        list(pool.map(probe, list(results)))
    print(f"埠掃描完成，耗時 {time.time() - t0:.1f} 秒")

    bind_ip = local_ip()
    if onvif_wait > 0 and bind_ip:
        print(f"ONVIF WS-Discovery 探測中（等待 {onvif_wait:g} 秒）...")
        targets = set(ips)
        for ip, info in onvif_discover(bind_ip, onvif_wait).items():
            if ip not in targets:
                continue
            entry = results.setdefault(ip, {"ports": [], "rtsp": [], "server": "", "title": "",
                                            "vendor": "", "onvif": None})
            entry["onvif"] = info
            entry["vendor"] = entry["vendor"] or guess_vendor(info["name"], info["hardware"]) \
                or info["name"]

    # 只保留看起來是攝影機的：RTSP 有回應、支援 ONVIF，或開了廠牌私有埠
    cameras = {
        ip: info for ip, info in results.items()
        if info["rtsp"] or info["onvif"] or any(p in VENDOR_PORTS for p in info["ports"])
    }
    return dict(sorted(cameras.items(), key=lambda kv: ipaddress.IPv4Address(kv[0])))


def print_table(cameras):
    if not cameras:
        print("\n找不到 IP 攝影機。可確認網段是否正確、攝影機是否已啟用 RTSP／ONVIF。")
        return
    rows = []
    for ip, info in cameras.items():
        model = ""
        if info["onvif"]:
            model = " ".join(filter(None, (info["onvif"]["name"], info["onvif"]["hardware"])))
        rows.append((
            ip,
            info["vendor"] or "?",
            ",".join(map(str, info["rtsp"])) or "-",
            "是" if info["onvif"] else "-",
            ",".join(map(str, info["ports"])) or "-",
            model or info["title"] or info["server"],
        ))
    headers = ("IP", "廠牌推測", "RTSP", "ONVIF", "開放埠", "型號／說明")
    widths = [max(_width(r[i]) for r in rows + [headers]) for i in range(len(headers))]
    print(f"\n找到 {len(cameras)} 台疑似 IP 攝影機：\n")
    for i, row in enumerate([headers] + rows):
        print("  ".join(_pad(cell, w) for cell, w in zip(row, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))


def _width(text):
    # 中文等全形字元在終端機佔兩格
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _pad(text, width):
    return str(text) + " " * (width - _width(text))


def export_yaml(cameras, path):
    """輸出可直接貼進 config.yaml 的 cameras 區塊。"""
    lines = ["# 由 scanner.py 產生；請填入密碼並確認 path 是否正確", "cameras:"]
    for i, (ip, info) in enumerate(cameras.items(), 1):
        port = info["rtsp"][0] if info["rtsp"] else 554
        vendor = info["vendor"] or "未知廠牌"
        lines += [
            f"  - name: cam{i}            # {vendor}",
            f"    host: {ip}",
            f"    port: {port}",
            "    username: admin",
            '    password: ""         # TODO: 填入密碼',
            f'    path: "{DEFAULT_PATHS.get(info["vendor"], FALLBACK_PATH)}"',
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已輸出設定到 {path}")


def main():
    parser = argparse.ArgumentParser(description="掃描區域網路中的 IP 攝影機")
    parser.add_argument("-s", "--subnet", action="append", type=parse_target, metavar="網段",
                        help="要掃描的網段，可重複指定；例如 192.168.1.0/24、192.168.1.10-50。"
                             "省略時掃描本機所在的 /24 網段")
    parser.add_argument("-p", "--ports", type=lambda s: tuple(int(p) for p in s.split(",")),
                        default=DEFAULT_PORTS, metavar="埠",
                        help=f"要測試的埠，以逗號分隔（預設 {','.join(map(str, DEFAULT_PORTS))}）")
    parser.add_argument("-t", "--timeout", type=float, default=0.5, help="每次連線逾時秒數（預設 0.5）")
    parser.add_argument("-w", "--workers", type=int, default=256, help="同時連線數（預設 256）")
    parser.add_argument("--onvif-wait", type=float, default=3.0,
                        help="等待 ONVIF 回應的秒數，0 表示不做 ONVIF 探測（預設 3）")
    parser.add_argument("-e", "--export", metavar="檔案",
                        help="把找到的攝影機輸出成 config.yaml 格式的 cameras 區塊")
    args = parser.parse_args()

    if args.subnet:
        ips = list(dict.fromkeys(ip for target in args.subnet for ip in target))
    else:
        net = default_network()
        print(f"未指定網段，使用本機所在網段 {net}")
        ips = list(net.hosts())
    if len(ips) > MAX_HOSTS:
        sys.exit(f"要掃描的位址有 {len(ips)} 個，超過上限 {MAX_HOSTS}，請縮小網段")

    cameras = scan(ips, args.ports, args.timeout, args.workers, args.onvif_wait)
    print_table(cameras)
    if args.export and cameras:
        export_yaml(cameras, args.export)


if __name__ == "__main__":
    main()
